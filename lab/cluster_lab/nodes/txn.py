"""Two-phase commit: a coordinator, some participants, and the prepared state.

The whole point of this model is the obligation a participant takes on when it
votes yes. It has persisted `prepared`, it is holding locks, and it may not
resolve on its own no matter how long it waits. Only a decision -- from the
original coordinator or from whoever can read the decision log -- releases it.
"""

from __future__ import annotations

from typing import Any

from ..kernel import Kernel
from ..network import Network
from ..trace import Trace

IDLE = "idle"
PREPARED = "prepared"
COMMITTED = "committed"
ABORTED = "aborted"
COORDINATOR = "coordinator"
BACKUP = "backup"

SINGLE = "single"
REPLICATED = "replicated"

CRASH_NONE = "none"
CRASH_BEFORE_DECISION = "before_decision"
CRASH_AFTER_DECISION = "after_decision"

STATUS_INTERVAL = 220


class DecisionLog:
    """Durable storage for decisions. Replicating it only changes who can read it."""

    def __init__(self) -> None:
        self.decisions: dict[str, str] = {}

    def record(self, txn: str, decision: str) -> None:
        self.decisions.setdefault(txn, decision)

    def get(self, txn: str) -> str | None:
        return self.decisions.get(txn)


class Participant:
    def __init__(self, node_id: str, system: "TwoPhaseSystem") -> None:
        self.id = node_id
        self.system = system
        self.alive = True
        self.data: dict[str, Any] = {}
        self.prepared_txn: str | None = None
        self.prepared_writes: dict[str, Any] = {}
        self.coordinator: str | None = None
        self.outcomes: dict[str, str] = {}

    @property
    def state(self) -> str:
        if self.prepared_txn:
            return PREPARED
        return IDLE

    def summary(self) -> str:
        if self.prepared_txn:
            keys = "、".join(sorted(self.prepared_writes))
            return f"持锁 {keys}（{self.prepared_txn}）"
        return " ".join(f"{key}={value}" for key, value in sorted(self.data.items())) or "空"

    def _record(self, state: str, txn: str = "") -> None:
        self.system.trace.record(self.system.kernel.now, "node.state", node=self.id,
                                 state=state, term=0, commit=0, votedFor=None,
                                 alive=self.alive, detail=self.summary(),
                                 reason=txn, lastIndex=0)

    def receive(self, source: str, message: dict[str, Any]) -> None:
        if not self.alive:
            return
        kind = message["type"]
        if kind == "Prepare":
            self._on_prepare(source, message)
        elif kind == "Decide":
            self._on_decide(message)

    def _on_prepare(self, source: str, message: dict[str, Any]) -> None:
        txn = message["txn"]
        if self.prepared_txn and self.prepared_txn != txn:
            # Locks from an unresolved transaction are exactly the blocking cost.
            self.system.trace.record(self.system.kernel.now, "txn.vote", node=self.id,
                                     txn=txn, ok=False,
                                     reason=f"键被 {self.prepared_txn} 的 prepared 事务锁住")
            self.system.network.send(self.id, source, {
                "type": "Vote", "txn": txn, "ok": False,
                "reason": f"{self.prepared_txn} 仍持锁",
            }, label="投否决票")
            return

        self.prepared_txn = txn
        self.prepared_writes = dict(message["writes"])
        self.coordinator = source
        self.system.trace.record(self.system.kernel.now, "txn.vote", node=self.id,
                                 txn=txn, ok=True, reason="持久化 prepared 并持锁")
        self._record(PREPARED, txn)
        self.system.network.send(self.id, source, {"type": "Vote", "txn": txn, "ok": True},
                                 label="投赞成票")
        self._schedule_status_query(txn)

    def _schedule_status_query(self, txn: str) -> None:
        def ask() -> None:
            if not self.alive or self.prepared_txn != txn:
                return
            target = self.system.resolver()
            if target:
                self.system.network.send(self.id, target,
                                         {"type": "StatusQuery", "txn": txn},
                                         label="询问决议")
            self.system.kernel.schedule(STATUS_INTERVAL, ask)

        self.system.kernel.schedule(STATUS_INTERVAL, ask)

    def _on_decide(self, message: dict[str, Any]) -> None:
        txn = message["txn"]
        decision = message["decision"]
        if self.outcomes.get(txn):
            return
        if self.prepared_txn != txn and decision == COMMITTED:
            return
        if decision == COMMITTED:
            self.data.update(self.prepared_writes)
        self.outcomes[txn] = decision
        if self.prepared_txn == txn:
            self.prepared_txn = None
            self.prepared_writes = {}
        self.system.trace.record(self.system.kernel.now, "txn.state", node=self.id,
                                 txn=txn, state=decision)
        self._record(decision, txn)


class Coordinator:
    def __init__(self, node_id: str, system: "TwoPhaseSystem", log: DecisionLog,
                 role: str = COORDINATOR) -> None:
        self.id = node_id
        self.system = system
        self.log = log
        self.role = role
        self.alive = role == COORDINATOR
        self.active = role == COORDINATOR
        self.votes: dict[str, dict[str, bool]] = {}
        self.clients: dict[str, dict[str, Any]] = {}
        self.participants_of: dict[str, list[str]] = {}

    def summary(self) -> str:
        known = "、".join(f"{txn}:{value}" for txn, value in sorted(self.log.decisions.items()))
        return known or "无决议"

    def _record(self, reason: str = "") -> None:
        state = self.role if self.active else BACKUP
        self.system.trace.record(self.system.kernel.now, "node.state", node=self.id,
                                 state=state, term=0, commit=0, votedFor=None,
                                 alive=self.alive, detail=self.summary(),
                                 reason=reason, lastIndex=0)

    def receive(self, source: str, message: dict[str, Any]) -> None:
        if not self.alive:
            return
        kind = message["type"]
        if kind == "BeginTxn":
            self._on_begin(source, message)
        elif kind == "Vote":
            self._on_vote(source, message)
        elif kind == "StatusQuery":
            self._on_status_query(source, message)

    def _on_begin(self, source: str, message: dict[str, Any]) -> None:
        txn = message["opId"]
        writes = message["writes"]
        self.participants_of[txn] = sorted(writes)
        self.votes[txn] = {}
        self.clients[txn] = {"client": source, "opId": message["opId"], "reqId": message["reqId"]}
        self.system.trace.record(self.system.kernel.now, "txn.begin", node=self.id,
                                 txn=txn, participants=sorted(writes))
        for participant, keys in sorted(writes.items()):
            self.system.network.send(self.id, participant, {
                "type": "Prepare", "txn": txn, "writes": keys,
            }, label=f"Prepare {txn}")

    def _on_vote(self, source: str, message: dict[str, Any]) -> None:
        txn = message["txn"]
        if txn not in self.votes or self.log.get(txn):
            return
        self.votes[txn][source] = message["ok"]
        expected = self.participants_of.get(txn, [])
        if not message["ok"]:
            self._decide(txn, ABORTED, f"{source} 投了否决票")
        elif len(self.votes[txn]) == len(expected) and all(self.votes[txn].values()):
            if (self.system.crash_point == CRASH_BEFORE_DECISION
                    and self.role == COORDINATOR and not self.system.crashed_once):
                self.system.trace.note(
                    self.system.kernel.now,
                    "全部参与者都投了赞成票，协调者却在把决议写入持久存储之前崩溃。"
                    "此刻没有任何决议存在，参与者却已经处于 prepared 并持有锁。",
                    level="warn", title="在写下决议之前崩溃")
                self.system.crash_coordinator()
                return
            self._decide(txn, COMMITTED, "全部投赞成票")

    def _decide(self, txn: str, decision: str, reason: str) -> None:
        if self.log.get(txn):
            decision = self.log.get(txn)
        else:
            # The decision has to be durable before anyone is told about it.
            self.log.record(txn, decision)
            self.system.trace.record(self.system.kernel.now, "txn.decision", node=self.id,
                                     txn=txn, decision=decision, reason=reason)
        self._record(f"决议 {txn}={decision}")

        if (self.system.crash_point == CRASH_AFTER_DECISION and self.role == COORDINATOR
                and not self.system.crashed_once):
            self.system.trace.note(
                self.system.kernel.now,
                f"决议 {decision} 已经持久化，协调者随后崩溃，通知还没发出去。"
                "事务其实已经提交，但客户端只会看到超时——这就是 unknown 的来源。",
                level="warn", title="决议已写下，通知未送出")
            self.system.crash_coordinator()
            return

        self._announce(txn, decision)

    def _announce(self, txn: str, decision: str) -> None:
        for participant in self.participants_of.get(txn) or self.system.participant_ids:
            self.system.network.send(self.id, participant, {
                "type": "Decide", "txn": txn, "decision": decision,
            }, label=f"{'Commit' if decision == COMMITTED else 'Abort'} {txn}")
        waiting = self.clients.pop(txn, None)
        if waiting:
            self.system.network.send(self.id, waiting["client"], {
                "type": "ClientReply",
                "status": "ok" if decision == COMMITTED else "unavailable",
                "reason": "事务已回滚", "opId": waiting["opId"], "reqId": waiting["reqId"],
                "result": decision,
            }, label=decision)

    def _on_status_query(self, source: str, message: dict[str, Any]) -> None:
        txn = message["txn"]
        decision = self.log.get(txn)
        if decision is None:
            self.system.trace.record(self.system.kernel.now, "txn.unresolved", node=self.id,
                                     txn=txn, asker=source)
            return
        self.system.network.send(self.id, source, {
            "type": "Decide", "txn": txn, "decision": decision,
        }, label=f"补发决议 {decision}")

    # -- recovery ----------------------------------------------------------------

    def take_over(self, participants: list[str]) -> None:
        self.alive = True
        self.active = True
        for txn in self.system.open_transactions():
            decision = self.log.get(txn)
            if decision is None:
                # Nobody was ever told to commit, so aborting is safe.
                self.log.record(txn, ABORTED)
                self.system.trace.record(self.system.kernel.now, "txn.decision", node=self.id,
                                         txn=txn, decision=ABORTED,
                                         reason="决议日志中没有该事务，可以安全回滚")
                decision = ABORTED
            self.participants_of.setdefault(txn, participants)
            self._announce(txn, decision)
        self._record("接管")


class TwoPhaseSystem:
    def __init__(self, kernel: Kernel, trace: Trace, network: Network,
                 participant_ids: list[str], coordinator_log: str = SINGLE,
                 crash_point: str = CRASH_NONE) -> None:
        self.kernel = kernel
        self.trace = trace
        self.network = network
        self.coordinator_log = coordinator_log
        self.crash_point = crash_point
        self.crashed_once = False
        self.participant_ids = list(participant_ids)

        self.log = DecisionLog()
        # Replicating the log does not create a second decision maker on its own;
        # it only means a standby can read what was already decided.
        backup_log = self.log if coordinator_log == REPLICATED else DecisionLog()

        self.coordinator = Coordinator("C1", self, self.log, COORDINATOR)
        self.backup = Coordinator("C2", self, backup_log, BACKUP)
        self.participants = {node_id: Participant(node_id, self) for node_id in participant_ids}

        trace.add_node("C1", kind="coordinator")
        network.attach("C1", self.coordinator.receive)
        self.backup.alive = coordinator_log == REPLICATED
        if self.backup.alive:
            # A standby only exists when there is a replicated log for it to read.
            trace.add_node("C2", kind="coordinator")
            network.attach("C2", self.backup.receive)
        for node_id, participant in self.participants.items():
            trace.add_node(node_id, kind="participant")
            network.attach(node_id, participant.receive)
            participant._record(IDLE)
        self.coordinator._record("启动")
        if self.backup.alive:
            self.backup._record("待命")

    def resolver(self) -> str | None:
        if self.coordinator.alive:
            return self.coordinator.id
        if self.backup.alive and self.backup.active:
            return self.backup.id
        return self.coordinator.id  # keep asking; nobody else may resolve this

    def crash_coordinator(self) -> None:
        self.crashed_once = True
        self.coordinator.alive = False
        self.network.down.add("C1")
        self.trace.record(self.kernel.now, "node.crash", node="C1", term=0, lastIndex=0)
        self.coordinator._record("崩溃")

    def restart_coordinator(self) -> None:
        if self.coordinator.alive:
            return
        self.coordinator.alive = True
        self.network.down.discard("C1")
        self.trace.record(self.kernel.now, "node.restart", node="C1", term=0, lastIndex=0)
        self.coordinator._record("重启后读取持久决议")
        self.coordinator.take_over(self.participant_ids)

    def promote_backup(self) -> None:
        if self.coordinator_log != REPLICATED:
            return
        self.network.down.discard("C2")
        self.backup.take_over(self.participant_ids)

    def open_transactions(self) -> list[str]:
        return sorted({participant.prepared_txn for participant in self.participants.values()
                       if participant.prepared_txn})

    def blocked(self) -> list[str]:
        return sorted(node_id for node_id, participant in self.participants.items()
                      if participant.prepared_txn)

    def snapshot(self) -> dict[str, Any]:
        result = {
            node_id: {
                "alive": participant.alive, "state": participant.state, "term": 0,
                "commit": 0, "base": 0, "log": [], "applied": [],
                "machine": participant.summary(), "outcomes": dict(participant.outcomes),
            }
            for node_id, participant in self.participants.items()
        }
        coordinators = [self.coordinator]
        if self.coordinator_log == REPLICATED:
            coordinators.append(self.backup)
        for coordinator in coordinators:
            result[coordinator.id] = {
                "alive": coordinator.alive, "state": coordinator.role, "term": 0,
                "commit": 0, "base": 0, "log": [], "applied": [],
                "machine": coordinator.summary(), "outcomes": dict(coordinator.log.decisions),
            }
        return result
