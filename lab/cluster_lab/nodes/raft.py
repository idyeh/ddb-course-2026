"""A teaching subset of Raft: elections, log replication, and the commit rules.

Deliberately incomplete. Persistence is modelled as "survives a crash", cluster
membership never changes, and snapshots arrive in a later phase. What it does
implement faithfully is the part the lecture argues about: the election
restriction, prefix repair, and the current-term commit rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from examples.raft_rules import can_advance_commit, candidate_is_up_to_date, majority

from ..kernel import Kernel
from ..network import Network
from ..trace import Trace
from .kv import StateMachine, label, noop

FOLLOWER = "follower"
CANDIDATE = "candidate"
LEADER = "leader"

# Two commit rules. `current_term` is Raft's; `naive_majority` is the mistake the
# course errata calls out, kept here so a class can watch it lose a committed entry.
CURRENT_TERM = "current_term"
NAIVE_MAJORITY = "naive_majority"

# Two acknowledgement points. `local` answers the client as soon as the leader has
# the entry on its own disk; `majority` waits until the entry is committed.
ACK_LOCAL = "local"
ACK_MAJORITY = "majority"

# Write policies. `consensus` is Raft. `any_node` is a teaching approximation of an
# AP system: every replica accepts the write locally and answers at once, so both
# sides of a partition stay writable and the losing side is reconciled away later.
WRITE_CONSENSUS = "consensus"
WRITE_ANY_NODE = "any_node"

# Read policies. Only the position of the reader changes; what changes the
# guarantee is what the reader has to prove before answering.
READ_LEADER_ONLY = "leader_only"
READ_INDEX = "readindex"
READ_LEASE = "lease"
READ_FOLLOWER = "follower_read"
READ_ANY_NODE = "any_node"


@dataclass
class Entry:
    term: int
    index: int
    command: dict[str, Any]


class RaftNode:
    def __init__(self, node_id: str, cluster: "RaftCluster") -> None:
        self.id = node_id
        self.cluster = cluster

        # Persistent state: survives a crash.
        self.current_term = 0
        self.voted_for: str | None = None
        self.log: list[Entry] = []
        # Compaction: entries at or below `log_base` live in the snapshot instead.
        self.log_base = 0
        self.snapshot_term = 0
        self.snapshot_state: dict[str, Any] | None = None

        # Volatile state: lost on a crash.
        self.state = FOLLOWER
        self.commit_index = 0
        self.last_applied = 0
        self.leader_id: str | None = None
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self.votes: set[str] = set()
        self.machine = StateMachine(owner=node_id, clock=lambda: cluster.kernel.now)
        self.pending: dict[int, dict[str, Any]] = {}  # log index -> waiting client request

        self.alive = True
        self._election_epoch = 0
        self._heartbeat_epoch = 0

        # Read path state, all volatile.
        self.lease_until = 0
        self._read_round = 0
        self._read_acks: dict[int, set[str]] = {}
        self._read_waiters: dict[int, list] = {}
        self._apply_waiters: list[tuple[int, object]] = []

    # -- helpers -----------------------------------------------------------------

    @property
    def peers(self) -> list[str]:
        return [node for node in self.cluster.node_ids if node != self.id]

    @property
    def last_index(self) -> int:
        return self.log_base + len(self.log)

    @property
    def last_term(self) -> int:
        return self.log[-1].term if self.log else self.snapshot_term

    def term_at(self, index: int) -> int:
        if index <= 0 or index > self.last_index:
            return 0
        if index == self.log_base:
            return self.snapshot_term
        if index < self.log_base:
            return -1  # compacted away; the leader must send a snapshot instead
        return self.log[index - self.log_base - 1].term

    def entry_at(self, index: int) -> Entry:
        return self.log[index - self.log_base - 1]

    def compact(self, up_to: int) -> None:
        """Discard the applied prefix, keeping the protocol position it stood at."""
        if up_to <= self.log_base or up_to > self.last_applied:
            return
        self.snapshot_term = self.term_at(up_to)
        self.snapshot_state = self.machine.capture()
        self.log = self.log[up_to - self.log_base:]
        self.log_base = up_to
        self.cluster.trace.record(self._now(), "log.snapshot", node=self.id,
                                  index=up_to, term=self.snapshot_term,
                                  state=self.machine.summary())

    def _now(self) -> int:
        return self.cluster.kernel.now

    def _clock(self) -> int:
        """What this node believes the time is; a skewed clock is a real fault."""
        return self.cluster.kernel.now + self.cluster.clock_offset.get(self.id, 0)

    def _send(self, target: str, message: dict[str, Any], text: str = "") -> None:
        message = {**message, "term": self.current_term}
        self.cluster.network.send(self.id, target, message, label=text)

    def _record_state(self, reason: str = "") -> None:
        self.cluster.trace.record(
            self._now(), "node.state", node=self.id, state=self.state,
            term=self.current_term, votedFor=self.voted_for, commit=self.commit_index,
            lastIndex=self.last_index, reason=reason, alive=self.alive,
        )

    # -- timers ------------------------------------------------------------------

    def reset_election_timer(self) -> None:
        self._election_epoch += 1
        epoch = self._election_epoch
        if not self.cluster.auto_election or not self.alive:
            return
        delay = self.cluster.kernel.rng.randrange(
            self.cluster.timeout_min, self.cluster.timeout_max
        )
        self.cluster.kernel.schedule(delay, lambda: self._on_election_timeout(epoch))

    def _on_election_timeout(self, epoch: int) -> None:
        if epoch != self._election_epoch or not self.alive or self.state == LEADER:
            return
        self.start_election()

    def _start_heartbeats(self) -> None:
        self._heartbeat_epoch += 1
        epoch = self._heartbeat_epoch
        self._beat(epoch)

    def _beat(self, epoch: int) -> None:
        if epoch != self._heartbeat_epoch or not self.alive or self.state != LEADER:
            return
        # Every heartbeat round doubles as a leadership probe: a round that reaches a
        # majority is what renews the lease and what a ReadIndex read waits for.
        self._read_round += 1
        round_id = self._read_round
        self._read_acks[round_id] = {self.id}
        for peer in self.peers:
            self.send_append(peer, read_id=round_id)
        self.cluster.kernel.schedule(self.cluster.heartbeat, lambda: self._beat(epoch))

    # -- elections ---------------------------------------------------------------

    def start_election(self) -> None:
        if not self.alive:
            return
        self.current_term += 1
        self.state = CANDIDATE
        self.voted_for = self.id
        self.votes = {self.id}
        self.leader_id = None
        self.cluster.elections += 1
        self._record_state("选举超时")
        self.reset_election_timer()
        for peer in self.peers:
            self._send(peer, {
                "type": "RequestVote",
                "candidate": self.id,
                "lastLogIndex": self.last_index,
                "lastLogTerm": self.last_term,
            }, text=f"term {self.current_term}")

    def become_follower(self, term: int, reason: str) -> None:
        changed = self.state != FOLLOWER or term != self.current_term
        self.state = FOLLOWER
        if term > self.current_term:
            self.current_term = term
            self.voted_for = None
        self.votes = set()
        if changed:
            self._record_state(reason)

    def become_leader(self) -> None:
        self.state = LEADER
        self.leader_id = self.id
        self.next_index = {peer: self.last_index + 1 for peer in self.peers}
        self.match_index = {peer: 0 for peer in self.peers}
        self.cluster.leader_changes += 1
        self._record_state("赢得多数票")
        self.cluster.trace.note(
            self._now(),
            f"{self.id} 在任期 (term) {self.current_term} 当选 Leader，"
            f"票数 {len(self.votes)}/{len(self.cluster.node_ids)}。",
            level="teach",
        )
        if self.cluster.leader_noop:
            # Raft §5.4.2: commit one entry of the current term before relying on the
            # commit position. Without it a freshly restarted leader believes its
            # commit index is 0 and would answer reads from an empty state machine.
            entry = Entry(self.current_term, self.last_index + 1, noop(self.current_term))
            self.log.append(entry)
            self.cluster.trace.record(
                self._now(), "log.append", node=self.id, index=entry.index,
                term=entry.term, cmd="noop", reqId=entry.command["reqId"],
                opId=entry.command["opId"],
            )
        self._start_heartbeats()

    # -- message handling --------------------------------------------------------

    def receive(self, source: str, message: dict[str, Any]) -> None:
        if not self.alive:
            return
        kind = message["type"]
        if kind == "ClientRequest":
            self._on_client_request(source, message)
            return
        if kind == "ClientRead":
            self._on_client_read(source, message)
            return

        term = message.get("term", 0)
        if term > self.current_term:
            self.become_follower(term, f"收到更高任期 {term}")

        handler = {
            "RequestVote": self._on_request_vote,
            "RequestVoteReply": self._on_request_vote_reply,
            "AppendEntries": self._on_append_entries,
            "InstallSnapshot": self._on_install_snapshot,
            "AppendEntriesReply": self._on_append_entries_reply,
            "ReadIndexRequest": self._on_read_index_request,
            "ReadIndexReply": self._on_read_index_reply,
        }[kind]
        handler(source, message)

    def _on_request_vote(self, source: str, message: dict[str, Any]) -> None:
        term = message["term"]
        granted = False
        if term == self.current_term and self.voted_for in (None, message["candidate"]):
            # The election restriction: a vote also asserts the candidate's log is
            # at least as up to date as the voter's.
            granted = candidate_is_up_to_date(
                message["lastLogTerm"], message["lastLogIndex"], self.last_term, self.last_index
            )
        if granted:
            self.voted_for = message["candidate"]
            self.reset_election_timer()
            self._record_state(f"投票给 {message['candidate']}")
        self._send(source, {"type": "RequestVoteReply", "granted": granted},
                   text="同意" if granted else "拒绝")

    def _on_request_vote_reply(self, source: str, message: dict[str, Any]) -> None:
        if self.state != CANDIDATE or message["term"] != self.current_term:
            return
        if not message["granted"]:
            return
        self.votes.add(source)
        if len(self.votes) >= majority(len(self.cluster.node_ids)):
            self.become_leader()

    def send_append(self, peer: str, read_id: int | None = None) -> None:
        next_index = self.next_index.get(peer, self.last_index + 1)
        prev_index = next_index - 1
        if next_index <= self.log_base:
            # The prefix this follower still needs was compacted away.
            self._send(peer, {
                "type": "InstallSnapshot", "leader": self.id,
                "lastIncludedIndex": self.log_base, "lastIncludedTerm": self.snapshot_term,
                "state": self.snapshot_state, "leaderCommit": self.commit_index,
            }, text=f"快照 ≤ {self.log_base}")
            return
        entries = [
            {"term": entry.term, "index": entry.index, "command": entry.command}
            for entry in self.log[prev_index - self.log_base:]
        ]
        self._send(peer, {
            "type": "AppendEntries",
            "leader": self.id,
            "prevLogIndex": prev_index,
            "prevLogTerm": self.term_at(prev_index),
            "entries": entries,
            "leaderCommit": self.commit_index,
            "readId": read_id,
        }, text=f"prev {prev_index}/{self.term_at(prev_index)} +{len(entries)}")

    def _on_append_entries(self, source: str, message: dict[str, Any]) -> None:
        term = message["term"]
        if term < self.current_term:
            self._send(source, {"type": "AppendEntriesReply", "success": False,
                                "matchIndex": 0, "readId": message.get("readId")},
                       text="任期过旧")
            return

        # A candidate that meets a legitimate leader of the current term steps down.
        self.become_follower(term, f"接受 {source} 为 Leader")
        self.leader_id = source
        self.reset_election_timer()

        prev_index = message["prevLogIndex"]
        if prev_index < self.log_base:
            # The prefix this message describes has already been compacted into our
            # snapshot. `term_at` cannot answer for it, and appending relative to it
            # would write inside the prefix. Tell the leader where our snapshot ends.
            self._send(source, {"type": "AppendEntriesReply", "success": False,
                                "matchIndex": 0, "compactedThrough": self.log_base,
                                "readId": message.get("readId")},
                       text=f"前缀已压缩 ≤ {self.log_base}")
            return
        if prev_index > self.last_index or self.term_at(prev_index) != message["prevLogTerm"]:
            self._send(source, {"type": "AppendEntriesReply", "success": False,
                                "matchIndex": 0, "readId": message.get("readId")},
                       text="前缀不匹配")
            return

        for offset, raw in enumerate(message["entries"]):
            index = prev_index + 1 + offset
            if index <= self.log_base:
                continue  # already durable inside the snapshot; never rewrite it
            if index <= self.last_index:
                if self.term_at(index) == raw["term"]:
                    continue
                del self.log[index - self.log_base - 1:]
                self.cluster.trace.record(self._now(), "log.truncate", node=self.id, **{"from": index})
            self.log.append(Entry(raw["term"], index, raw["command"]))
            self.cluster.trace.record(
                self._now(), "log.append", node=self.id, index=index, term=raw["term"],
                cmd=label(raw["command"]), reqId=raw["command"]["reqId"],
                opId=raw["command"]["opId"],
            )
            if self.cluster.write_policy == WRITE_ANY_NODE:
                # No commit barrier in this model: whatever arrives is applied at once.
                result, deduplicated = self.machine.apply(raw["command"])
                self.cluster.trace.record(
                    self._now(), "state.apply", node=self.id, index=index,
                    opId=raw["command"]["opId"], reqId=raw["command"]["reqId"],
                    deduped=deduplicated, state=self.machine.summary(),
                )

        match_index = prev_index + len(message["entries"])
        if message["leaderCommit"] > self.commit_index:
            self.commit_index = min(message["leaderCommit"], self.last_index)
            self._apply_committed()
        self._send(source, {"type": "AppendEntriesReply", "success": True,
                            "matchIndex": match_index, "readId": message.get("readId")},
                   text=f"匹配到 {match_index}")

    def _on_append_entries_reply(self, source: str, message: dict[str, Any]) -> None:
        if self.state != LEADER or message["term"] != self.current_term:
            return
        self._count_read_ack(source, message.get("readId"))
        if message["success"]:
            self.match_index[source] = max(self.match_index.get(source, 0), message["matchIndex"])
            self.next_index[source] = self.match_index[source] + 1
            self._advance_commit()
        else:
            compacted = message.get("compactedThrough")
            if compacted:
                # The follower told us exactly where its snapshot ends; resume there
                # instead of walking back one index at a time into a discarded prefix.
                self.next_index[source] = compacted + 1
            else:
                # Walk `nextIndex` back until the prefixes agree, then append the suffix.
                self.next_index[source] = max(1, self.next_index.get(source, 1) - 1)
            self.send_append(source)

    # -- commit and apply --------------------------------------------------------

    def _advance_commit(self) -> None:
        matches = [self.last_index] + [self.match_index.get(peer, 0) for peer in self.peers]
        # Pad so ordinary 1-based index arithmetic still works after compaction.
        log_terms = [0] * self.log_base + [entry.term for entry in self.log]
        highest = self.commit_index
        for index in range(self.commit_index + 1, self.last_index + 1):
            if self.cluster.commit_rule == CURRENT_TERM:
                ok = can_advance_commit(matches, log_terms, index, self.current_term)
            else:
                replicated = sum(match >= index for match in matches)
                ok = replicated >= majority(len(matches))
            if ok:
                highest = index
        if highest > self.commit_index:
            self.commit_index = highest
            self._apply_committed()

    def _apply_committed(self) -> None:
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self.entry_at(self.last_applied)
            result, deduplicated = self.machine.apply(entry.command)
            if self.state == LEADER:
                self.cluster.trace.record(
                    self._now(), "log.commit", node=self.id, index=entry.index,
                    term=entry.term, cmd=label(entry.command),
                    opId=entry.command["opId"], rule=self.cluster.commit_rule,
                )
            self.cluster.trace.record(
                self._now(), "state.apply", node=self.id, index=entry.index,
                opId=entry.command["opId"], reqId=entry.command["reqId"],
                deduped=deduplicated, state=self.machine.summary(),
            )
            waiting = self.pending.pop(entry.index, None)
            if waiting is not None and self.state == LEADER:
                self._reply_ok(waiting, result, entry.index, entry.term)
        threshold = self.cluster.snapshot_threshold
        if threshold and self.last_applied - self.log_base >= threshold:
            self.compact(self.last_applied)
        ready = [waiter for index, waiter in self._apply_waiters if index <= self.last_applied]
        self._apply_waiters = [(index, waiter) for index, waiter in self._apply_waiters
                               if index > self.last_applied]
        for waiter in ready:
            waiter()

    def _on_install_snapshot(self, source: str, message: dict[str, Any]) -> None:
        if message["term"] < self.current_term:
            return
        self.become_follower(message["term"], f"接受 {source} 的快照")
        self.leader_id = source
        self.reset_election_timer()
        if message["lastIncludedIndex"] <= self.log_base:
            self._send(source, {"type": "AppendEntriesReply", "success": True,
                                "matchIndex": self.log_base, "readId": None}, text="快照已过时")
            return

        self.log = []
        self.log_base = message["lastIncludedIndex"]
        self.snapshot_term = message["lastIncludedTerm"]
        self.snapshot_state = message["state"]
        self.machine.restore(message["state"])
        # A snapshot only ever covers applied entries, so both positions move with it.
        self.commit_index = max(self.commit_index, self.log_base)
        self.last_applied = self.log_base
        self.cluster.trace.record(self._now(), "log.snapshot", node=self.id,
                                  index=self.log_base, term=self.snapshot_term,
                                  installed=True, state=self.machine.summary())
        self.cluster.trace.record(self._now(), "state.apply", node=self.id,
                                  index=self.log_base, opId="snapshot", reqId="snapshot",
                                  deduped=False, state=self.machine.summary())
        self._record_state("安装快照")
        self._send(source, {"type": "AppendEntriesReply", "success": True,
                            "matchIndex": self.log_base, "readId": None},
                   text=f"已安装 ≤ {self.log_base}")

    # -- read path ---------------------------------------------------------------

    def _count_read_ack(self, source: str, read_id: int | None) -> None:
        if read_id is None or read_id not in self._read_acks:
            return
        acks = self._read_acks[read_id]
        acks.add(source)
        if len(acks) < majority(len(self.cluster.node_ids)):
            return
        # A majority answered in this term, so no other leader can have been elected
        # before this moment: the lease is renewed and pending reads may proceed.
        self.lease_until = self._clock() + self.cluster.lease
        for waiter in self._read_waiters.pop(read_id, []):
            waiter()
        self._read_acks.pop(read_id, None)

    def _confirm_leadership(self, callback) -> None:
        self._read_round += 1
        round_id = self._read_round
        self._read_acks[round_id] = {self.id}
        self._read_waiters[round_id] = [callback]
        for peer in self.peers:
            self.send_append(peer, read_id=round_id)
        if len(self.cluster.node_ids) == 1:
            self._count_read_ack(self.id, round_id)

    def _wait_applied(self, index: int, callback) -> None:
        if self.last_applied >= index:
            callback()
        else:
            self._apply_waiters.append((index, callback))

    def _read_value(self, key: str) -> Any:
        return self.machine.kv.get(key)

    def _reply_read(self, requester: str, message: dict[str, Any], note: str) -> None:
        self._send(requester, {
            "type": "ClientReply", "status": "ok", "result": self._read_value(message["key"]),
            "opId": message["opId"], "reqId": message["reqId"], "servedBy": self.id,
        }, text=note)

    def _refuse_read(self, requester: str, message: dict[str, Any], reason: str) -> None:
        self.cluster.trace.record(self._now(), "read.refuse", node=self.id,
                                  opId=message["opId"], reason=reason)
        self._send(requester, {"type": "ClientReply", "status": "unavailable",
                               "reason": reason, "opId": message["opId"],
                               "reqId": message["reqId"]}, text="拒绝服务")

    def _on_client_read(self, source: str, message: dict[str, Any]) -> None:
        policy = self.cluster.read_policy
        self.cluster.trace.record(self._now(), "read.begin", node=self.id,
                                  opId=message["opId"], policy=policy, state=self.state)

        if policy == READ_ANY_NODE:
            self._reply_read(source, message, "本地直接读")
            return

        if policy == READ_FOLLOWER:
            if self.state == LEADER:
                self._confirm_leadership(
                    lambda: self._wait_applied(self.commit_index,
                                               lambda: self._reply_read(source, message, "ReadIndex")))
                return
            if not self.leader_id:
                self._refuse_read(source, message, "不知道谁是 Leader")
                return
            self._send(self.leader_id, {"type": "ReadIndexRequest", "requester": source,
                                        "read": message}, text="请求 ReadIndex")
            return

        if self.state != LEADER:
            self._send(source, {"type": "ClientReply", "status": "redirect",
                                "leader": self.leader_id, "opId": message["opId"],
                                "reqId": message["reqId"]}, text="非 Leader")
            return

        if policy == READ_LEADER_ONLY:
            # No proof of leadership at all: a stale leader answers happily.
            self._reply_read(source, message, "直接从 Leader 读")
        elif policy == READ_LEASE:
            if self._clock() < self.lease_until:
                self._reply_read(source, message, "租约内读")
            else:
                self._refuse_read(source, message, "租约已过期")
        else:  # READ_INDEX
            read_index = self.commit_index
            self._confirm_leadership(
                lambda: self._wait_applied(read_index,
                                           lambda: self._reply_read(source, message, "ReadIndex")))

    def _on_read_index_request(self, source: str, message: dict[str, Any]) -> None:
        read = message["read"]
        if self.state != LEADER:
            self._send(source, {"type": "ReadIndexReply", "ok": False, "read": read,
                                "requester": message["requester"]}, text="我不是 Leader")
            return
        read_index = self.commit_index
        self._confirm_leadership(lambda: self._send(source, {
            "type": "ReadIndexReply", "ok": True, "index": read_index, "read": read,
            "requester": message["requester"],
        }, text=f"ReadIndex {read_index}"))

    def _on_read_index_reply(self, source: str, message: dict[str, Any]) -> None:
        read = message["read"]
        requester = message["requester"]
        if not message.get("ok"):
            self._refuse_read(requester, read, "Leader 未确认领导权")
            return
        # Answer only once this replica has caught up to the leader's commit position.
        self._wait_applied(message["index"],
                           lambda: self._reply_read(requester, read, "Follower Read"))

    # -- client path -------------------------------------------------------------

    def _on_client_request(self, source: str, message: dict[str, Any]) -> None:
        if self.state != LEADER and self.cluster.write_policy != WRITE_ANY_NODE:
            self._send(source, {"type": "ClientReply", "status": "redirect",
                                "leader": self.leader_id, "opId": message["opId"],
                                "reqId": message["reqId"]}, text="非 Leader")
            return

        command = message["command"]
        if command["reqId"] in self.machine.results:
            # The dedup table is replicated state, so a retry is answered from it.
            self.cluster.trace.note(
                self._now(),
                f"{self.id} 在去重表 (dedup table) 中找到 {command['reqId']}，"
                "直接返回旧结果，不会重复执行。",
                level="teach",
            )
            self._send(source, {"type": "ClientReply", "status": "ok",
                                "result": self.machine.results[command["reqId"]],
                                "opId": message["opId"], "reqId": command["reqId"],
                                "deduped": True}, text="去重命中")
            return

        if self.cluster.write_policy == WRITE_ANY_NODE:
            # Teaching approximation of an AP write: accept locally, answer at once.
            index = self.last_index + 1
            self.log.append(Entry(self.current_term, index, command))
            self.cluster.trace.record(
                self._now(), "log.append", node=self.id, index=index, term=self.current_term,
                cmd=label(command), reqId=command["reqId"], opId=command["opId"],
            )
            result, deduplicated = self.machine.apply(command)
            self.cluster.trace.record(
                self._now(), "state.apply", node=self.id, index=index,
                opId=command["opId"], reqId=command["reqId"], deduped=deduplicated,
                state=self.machine.summary(),
            )
            self._reply_ok({"client": source, "opId": message["opId"],
                            "reqId": command["reqId"]}, result, index, self.current_term,
                           acked_locally=True)
            if self.state == LEADER:
                for peer in self.peers:
                    self.send_append(peer)
            return

        index = self.last_index + 1
        self.log.append(Entry(self.current_term, index, command))
        self.cluster.trace.record(
            self._now(), "log.append", node=self.id, index=index, term=self.current_term,
            cmd=label(command), reqId=command["reqId"], opId=command["opId"],
        )
        request = {"client": source, "opId": message["opId"], "reqId": command["reqId"]}
        if self.cluster.ack == ACK_LOCAL:
            self._reply_ok(request, None, index, self.current_term, acked_locally=True)
        else:
            self.pending[index] = request
        for peer in self.peers:
            self.send_append(peer)

    def _reply_ok(self, request: dict[str, Any], result: Any, index: int, term: int,
                  acked_locally: bool = False) -> None:
        self._send(request["client"], {
            "type": "ClientReply", "status": "ok", "result": result,
            "opId": request["opId"], "reqId": request["reqId"],
            "index": index, "logTerm": term, "ackedLocally": acked_locally,
        }, text="ok")

    # -- faults ------------------------------------------------------------------

    def crash(self) -> None:
        if not self.alive:
            return
        self.alive = False
        # Volatile state is lost; term, vote and log survive.
        self.state = FOLLOWER
        self.commit_index = 0
        self.last_applied = 0
        self.leader_id = None
        self.votes = set()
        self.next_index = {}
        self.match_index = {}
        self.pending = {}
        self.lease_until = 0
        self._read_acks = {}
        self._read_waiters = {}
        self._apply_waiters = []
        self.machine.reset()
        self._election_epoch += 1
        self._heartbeat_epoch += 1
        self.cluster.network.down.add(self.id)
        self.cluster.trace.record(self._now(), "node.crash", node=self.id,
                                  term=self.current_term, lastIndex=self.last_index)
        self._record_state("崩溃")

    def restart(self) -> None:
        if self.alive:
            return
        self.alive = True
        if self.snapshot_state is not None:
            # The snapshot is on disk: it survives, and so does the position it binds.
            self.machine.restore(self.snapshot_state)
            self.commit_index = self.log_base
            self.last_applied = self.log_base
        self.cluster.network.down.discard(self.id)
        self.cluster.trace.record(self._now(), "node.restart", node=self.id,
                                  term=self.current_term, lastIndex=self.last_index)
        self._record_state("重启")
        self.reset_election_timer()


class RaftCluster:
    def __init__(
        self,
        kernel: Kernel,
        trace: Trace,
        network: Network,
        node_ids: list[str],
        heartbeat: int = 40,
        timeout_min: int = 150,
        timeout_max: int = 300,
        commit_rule: str = CURRENT_TERM,
        ack: str = ACK_MAJORITY,
        auto_election: bool = True,
        leader_noop: bool = True,
        snapshot_threshold: int = 0,
        read_policy: str = READ_INDEX,
        write_policy: str = WRITE_CONSENSUS,
        lease: int = 220,
        clock_offset: dict[str, int] | None = None,
    ) -> None:
        self.kernel = kernel
        self.trace = trace
        self.network = network
        self.node_ids = list(node_ids)
        self.heartbeat = heartbeat
        self.timeout_min = timeout_min
        self.timeout_max = timeout_max
        self.commit_rule = commit_rule
        self.ack = ack
        self.auto_election = auto_election
        self.leader_noop = leader_noop
        self.snapshot_threshold = snapshot_threshold
        self.read_policy = read_policy
        self.write_policy = write_policy
        self.lease = lease
        self.clock_offset = dict(clock_offset or {})
        self.elections = 0
        self.leader_changes = 0

        self.nodes = {node_id: RaftNode(node_id, self) for node_id in self.node_ids}
        for node_id, node in self.nodes.items():
            trace.add_node(node_id, kind="replica")
            network.attach(node_id, node.receive)
            node._record_state("启动")
            node.reset_election_timer()

    # -- control surface used by the scenarios -----------------------------------

    def leader(self) -> RaftNode | None:
        leaders = [node for node in self.nodes.values() if node.alive and node.state == LEADER]
        if not leaders:
            return None
        return max(leaders, key=lambda node: node.current_term)

    def force_election(self, node_id: str) -> None:
        self.nodes[node_id].start_election()

    def crash(self, node_id: str) -> None:
        self.nodes[node_id].crash()

    def restart(self, node_id: str) -> None:
        self.nodes[node_id].restart()

    def snapshot(self) -> dict[str, Any]:
        return {
            node_id: {
                "alive": node.alive,
                "state": node.state,
                "term": node.current_term,
                "commit": node.commit_index,
                "base": node.log_base,
                "log": [
                    {"index": entry.index, "term": entry.term,
                     "cmd": label(entry.command), "opId": entry.command["opId"]}
                    for entry in node.log
                ],
                "applied": list(node.machine.applied),
                "machine": node.machine.summary(),
            }
            for node_id, node in self.nodes.items()
        }
