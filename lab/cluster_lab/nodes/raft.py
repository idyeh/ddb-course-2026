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
from .kv import StateMachine, label

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

        # Volatile state: lost on a crash.
        self.state = FOLLOWER
        self.commit_index = 0
        self.last_applied = 0
        self.leader_id: str | None = None
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}
        self.votes: set[str] = set()
        self.machine = StateMachine()
        self.pending: dict[int, dict[str, Any]] = {}  # log index -> waiting client request

        self.alive = True
        self._election_epoch = 0
        self._heartbeat_epoch = 0

    # -- helpers -----------------------------------------------------------------

    @property
    def peers(self) -> list[str]:
        return [node for node in self.cluster.node_ids if node != self.id]

    @property
    def last_index(self) -> int:
        return len(self.log)

    @property
    def last_term(self) -> int:
        return self.log[-1].term if self.log else 0

    def term_at(self, index: int) -> int:
        if index <= 0 or index > len(self.log):
            return 0
        return self.log[index - 1].term

    def _now(self) -> int:
        return self.cluster.kernel.now

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
        for peer in self.peers:
            self.send_append(peer)
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
        self._start_heartbeats()

    # -- message handling --------------------------------------------------------

    def receive(self, source: str, message: dict[str, Any]) -> None:
        if not self.alive:
            return
        kind = message["type"]
        if kind == "ClientRequest":
            self._on_client_request(source, message)
            return

        term = message.get("term", 0)
        if term > self.current_term:
            self.become_follower(term, f"收到更高任期 {term}")

        handler = {
            "RequestVote": self._on_request_vote,
            "RequestVoteReply": self._on_request_vote_reply,
            "AppendEntries": self._on_append_entries,
            "AppendEntriesReply": self._on_append_entries_reply,
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

    def send_append(self, peer: str) -> None:
        next_index = self.next_index.get(peer, self.last_index + 1)
        prev_index = next_index - 1
        entries = [
            {"term": entry.term, "index": entry.index, "command": entry.command}
            for entry in self.log[prev_index:]
        ]
        self._send(peer, {
            "type": "AppendEntries",
            "leader": self.id,
            "prevLogIndex": prev_index,
            "prevLogTerm": self.term_at(prev_index),
            "entries": entries,
            "leaderCommit": self.commit_index,
        }, text=f"prev {prev_index}/{self.term_at(prev_index)} +{len(entries)}")

    def _on_append_entries(self, source: str, message: dict[str, Any]) -> None:
        term = message["term"]
        if term < self.current_term:
            self._send(source, {"type": "AppendEntriesReply", "success": False,
                                "matchIndex": 0}, text="任期过旧")
            return

        # A candidate that meets a legitimate leader of the current term steps down.
        self.become_follower(term, f"接受 {source} 为 Leader")
        self.leader_id = source
        self.reset_election_timer()

        prev_index = message["prevLogIndex"]
        if prev_index > self.last_index or self.term_at(prev_index) != message["prevLogTerm"]:
            self._send(source, {"type": "AppendEntriesReply", "success": False,
                                "matchIndex": 0}, text="前缀不匹配")
            return

        for offset, raw in enumerate(message["entries"]):
            index = prev_index + 1 + offset
            if index <= self.last_index:
                if self.term_at(index) == raw["term"]:
                    continue
                del self.log[index - 1:]
                self.cluster.trace.record(self._now(), "log.truncate", node=self.id, **{"from": index})
            self.log.append(Entry(raw["term"], index, raw["command"]))
            self.cluster.trace.record(
                self._now(), "log.append", node=self.id, index=index, term=raw["term"],
                cmd=label(raw["command"]), reqId=raw["command"]["reqId"],
                opId=raw["command"]["opId"],
            )

        match_index = prev_index + len(message["entries"])
        if message["leaderCommit"] > self.commit_index:
            self.commit_index = min(message["leaderCommit"], self.last_index)
            self._apply_committed()
        self._send(source, {"type": "AppendEntriesReply", "success": True,
                            "matchIndex": match_index}, text=f"匹配到 {match_index}")

    def _on_append_entries_reply(self, source: str, message: dict[str, Any]) -> None:
        if self.state != LEADER or message["term"] != self.current_term:
            return
        if message["success"]:
            self.match_index[source] = max(self.match_index.get(source, 0), message["matchIndex"])
            self.next_index[source] = self.match_index[source] + 1
            self._advance_commit()
        else:
            # Walk `nextIndex` back until the prefixes agree, then append the suffix.
            self.next_index[source] = max(1, self.next_index.get(source, 1) - 1)
            self.send_append(source)

    # -- commit and apply --------------------------------------------------------

    def _advance_commit(self) -> None:
        matches = [self.last_index] + [self.match_index.get(peer, 0) for peer in self.peers]
        log_terms = [entry.term for entry in self.log]
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
            entry = self.log[self.last_applied - 1]
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

    # -- client path -------------------------------------------------------------

    def _on_client_request(self, source: str, message: dict[str, Any]) -> None:
        if self.state != LEADER:
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
