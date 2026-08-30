"""Where does coordination happen: during execution, or before it?

Three ways to run the same cross-shard transfers.

- `none`      : send each half straight to its shard. Fewest messages, no atomicity.
- `coordinate`: two-phase commit around every transaction.
- `preorder`  : a sequencer fixes a global order, replicates the input, and each
                shard then executes that order locally. Atomicity comes from
                determinism -- but the input still has to be agreed on, and a
                recovering shard replays it.
"""

from __future__ import annotations

from typing import Any

NONE = "none"
COORDINATE = "coordinate"
PREORDER = "preorder"

KNOWN = "known"
DEPENDENT = "dependent"

START_BALANCE = 1000


class Shard:
    def __init__(self, node_id: str, system: "OrderingSystem") -> None:
        self.id = node_id
        self.system = system
        self.balance = START_BALANCE
        self.lock: str | None = None
        self.alive = True
        self.applied: set[str] = set()
        self.done_batches: set[int] = set()

    def summary(self) -> str:
        return f"余额 {self.balance}" + (f"（锁 {self.lock}）" if self.lock else "")

    def _record(self, reason: str = "") -> None:
        self.system.trace.record(self.system.kernel.now, "node.state", node=self.id,
                                 state="locked" if self.lock else "idle", term=0, commit=0,
                                 votedFor=None, alive=self.alive, detail=self.summary(),
                                 reason=reason, lastIndex=0)

    def apply(self, txn: str, delta: int) -> None:
        if txn in self.applied:
            return
        self.applied.add(txn)
        self.balance += delta
        self.system.trace.record(self.system.kernel.now, "txn.state", node=self.id,
                                 txn=txn, state="committed", delta=delta)
        self._record(f"应用 {txn}")

    def receive(self, source: str, message: dict[str, Any]) -> None:
        if not self.alive:
            return
        kind = message["type"]
        if kind == "DirectApply":
            self.apply(message["txn"], message["delta"])
            self.system.network.send(self.id, source, {"type": "DirectAck",
                                                       "txn": message["txn"]}, label="ok")
        elif kind == "Prepare":
            free = self.lock is None or self.lock == message["txn"]
            if free:
                self.lock = message["txn"]
                self._record(f"为 {message['txn']} 加锁")
            self.system.network.send(self.id, source, {
                "type": "Vote", "txn": message["txn"], "ok": free,
            }, label="赞成" if free else "否决")
        elif kind == "Decide":
            if message["commit"]:
                self.apply(message["txn"], message["delta"])
            if self.lock == message["txn"]:
                self.lock = None
            self._record("释放锁")
            self.system.network.send(self.id, source, {"type": "Ack", "txn": message["txn"]},
                                     label="ack")
        elif kind == "ExecuteBatch":
            self._execute_batch(source, message)

    def _execute_batch(self, source: str, message: dict[str, Any]) -> None:
        if message["batch"] in self.done_batches:
            return
        self.done_batches.add(message["batch"])
        # Same order on every shard, so no cross-shard message is needed here.
        for entry in message["txns"]:
            delta = entry["deltas"].get(self.id)
            if delta is not None:
                self.apply(entry["txn"], delta)
        self.system.network.send(self.id, source, {"type": "BatchDone",
                                                   "batch": message["batch"]}, label="批次完成")

    def crash(self) -> None:
        self.alive = False
        self.lock = None
        self.system.network.down.add(self.id)
        self.system.trace.record(self.system.kernel.now, "node.crash", node=self.id,
                                 term=0, lastIndex=0)
        self._record("崩溃")

    def restart(self) -> None:
        self.alive = True
        self.system.network.down.discard(self.id)
        self.system.trace.record(self.system.kernel.now, "node.restart", node=self.id,
                                 term=0, lastIndex=0)
        self._record("重启")
        if self.system.mode == COORDINATE:
            # A recovering participant asks for the decisions it slept through.
            self.system.trace.note(
                self.system.kernel.now,
                f"{self.id} 重启后向协调者询问它错过的决议。"
                "prepared 参与者不能自行了结，恢复后必须把决议补上——"
                "否则事务就只生效了一半。",
                level="teach", title="补上错过的决议")
            self.system.network.send(self.id, self.system.coordinator,
                                     {"type": "RecoverQuery"}, label="询问决议")
        if self.system.mode == PREORDER:
            # The input log was replicated, so recovery is a replay of it.
            missed = [batch for batch in self.system.batches
                      if batch["batch"] not in self.done_batches]
            self.system.trace.note(
                self.system.kernel.now,
                f"{self.id} 重启后从已复制的输入日志重放了 {len(missed)} 个批次。"
                "确定性执行的恢复手段就是重放同一份输入。",
                level="teach", title="重放输入日志")
            for batch in missed:
                self._execute_batch(self.system.sequencer, batch)


class OrderingSystem:
    def __init__(self, kernel: Any, trace: Any, network: Any, shard_ids: list[str],
                 mode: str, rw_set: str = KNOWN) -> None:
        self.kernel = kernel
        self.trace = trace
        self.network = network
        self.mode = mode
        self.rw_set = rw_set
        self.shard_ids = list(shard_ids)
        self.sequencer = "SEQ"
        self.coordinator = "CO"
        self.shards = {shard_id: Shard(shard_id, self) for shard_id in shard_ids}
        for shard_id, shard in self.shards.items():
            trace.add_node(shard_id, kind="shard")
            network.attach(shard_id, shard.receive)
            shard._record("启动")

        self.round_trips = 0
        self.aborts = 0
        self.committed: set[str] = set()
        self.recon_trips = 0
        self.decisions: dict[str, bool] = {}
        self.pending_acks: dict[str, set[str]] = {}
        self.batch_acks: dict[int, set[str]] = {}
        self.retransmits = 0
        self.batches: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.votes: dict[str, dict[str, bool]] = {}
        self.plans: dict[str, dict[str, Any]] = {}

        if mode == COORDINATE:
            trace.add_node(self.coordinator, kind="coordinator")
            network.attach(self.coordinator, self._coordinator_receive)
            trace.record(kernel.now, "node.state", node=self.coordinator, state="coordinator",
                         term=0, commit=0, votedFor=None, alive=True, detail="无进行中事务",
                         reason="启动", lastIndex=0)
        elif mode == PREORDER:
            trace.add_node(self.sequencer, kind="coordinator")
            network.attach(self.sequencer, self._sequencer_receive)
            trace.record(kernel.now, "node.state", node=self.sequencer, state="coordinator",
                         term=0, commit=0, votedFor=None, alive=True, detail="等待事务",
                         reason="启动", lastIndex=0)

    # -- submission ---------------------------------------------------------------

    def submit(self, txn: str, source: str, target: str, amount: int) -> None:
        deltas = {source: -amount, target: amount}
        self.plans[txn] = {"txn": txn, "deltas": deltas, "at": self.kernel.now}
        self.trace.record(self.kernel.now, "txn.begin", node=self.mode, txn=txn,
                          participants=sorted(deltas))
        if self.mode == NONE:
            self.round_trips += 1
            for shard_id, delta in sorted(deltas.items()):
                self.network.send("客户端", shard_id, {"type": "DirectApply", "txn": txn,
                                                    "delta": delta}, label=f"{delta:+d}")
            self.committed.add(txn)
        elif self.mode == COORDINATE:
            self.votes[txn] = {}
            self.round_trips += 2  # prepare/vote, then decide/ack
            for shard_id, delta in sorted(deltas.items()):
                self.network.send(self.coordinator, shard_id,
                                  {"type": "Prepare", "txn": txn}, label=f"Prepare {txn}")
        else:
            if self.rw_set == DEPENDENT:
                # The write set depends on a read, so it has to be discovered first.
                self.recon_trips += 1
                self.trace.record(self.kernel.now, "txn.recon", txn=txn)
            self.pending.append(self.plans[txn])

    # -- 2PC ----------------------------------------------------------------------

    def _coordinator_receive(self, source: str, message: dict[str, Any]) -> None:
        if message["type"] == "Ack":
            self.pending_acks.get(message["txn"], set()).discard(source)
            return
        if message["type"] == "RecoverQuery":
            for txn, commit in sorted(self.decisions.items()):
                delta = self.plans[txn]["deltas"].get(source)
                if delta is None:
                    continue
                self.network.send(self.coordinator, source, {
                    "type": "Decide", "txn": txn, "commit": commit, "delta": delta,
                }, label="补发决议")
            return
        if message["type"] != "Vote":
            return
        txn = message["txn"]
        if txn not in self.votes:
            return
        self.votes[txn][source] = message["ok"]
        plan = self.plans[txn]
        if not message["ok"]:
            self._decide(txn, False)
        elif len(self.votes[txn]) == len(plan["deltas"]):
            self._decide(txn, True)

    def _decide(self, txn: str, commit: bool) -> None:
        if txn not in self.votes:
            return
        del self.votes[txn]
        self.decisions[txn] = commit
        self.pending_acks[txn] = set(self.plans[txn]["deltas"])
        if commit:
            self.committed.add(txn)
        else:
            self.aborts += 1
        for shard_id, delta in sorted(self.plans[txn]["deltas"].items()):
            self.network.send(self.coordinator, shard_id, {
                "type": "Decide", "txn": txn, "commit": commit, "delta": delta,
            }, label="Commit" if commit else "Abort")
        self.trace.record(self.kernel.now, "txn.decision", node=self.coordinator, txn=txn,
                          decision="committed" if commit else "aborted",
                          reason="全部赞成" if commit else "有参与者否决")

    def sweep(self, timeout: int = 300) -> None:
        """A coordinator does not forget: it retries decisions until they are acked.

        It also gives up on votes that never arrive, which is what releases the
        locks the other participant is still holding.
        """
        for txn in list(self.votes):
            waiting = [shard_id for shard_id in self.plans[txn]["deltas"]
                       if shard_id not in self.votes[txn]]
            stale = self.kernel.now - self.plans[txn]["at"] > timeout
            if stale or any(not self.shards[shard_id].alive for shard_id in waiting):
                self._decide(txn, False)

        for txn, outstanding in list(self.pending_acks.items()):
            for shard_id in sorted(outstanding):
                if not self.shards[shard_id].alive:
                    continue
                self.retransmits += 1
                self.network.send(self.coordinator, shard_id, {
                    "type": "Decide", "txn": txn, "commit": self.decisions[txn],
                    "delta": self.plans[txn]["deltas"][shard_id],
                }, label="重发决议")

    def resend_batches(self) -> None:
        """The sequenced input is durable, so a shard that missed it gets it again."""
        for batch in self.batches:
            for shard_id in self.shard_ids:
                if shard_id in self.batch_acks.get(batch["batch"], set()):
                    continue
                if not self.shards[shard_id].alive:
                    continue
                self.retransmits += 1
                self.network.send(self.sequencer, shard_id, batch,
                                  label=f"重发批次 {batch['batch']}")

    # -- Calvin -------------------------------------------------------------------

    def seal_batch(self) -> None:
        if self.mode != PREORDER or not self.pending:
            return
        batch = {"type": "ExecuteBatch", "batch": len(self.batches) + 1,
                 "txns": list(self.pending)}
        self.batches.append(batch)
        self.pending = []
        self.round_trips += 1  # one replication round for the whole batch
        self.trace.record(self.kernel.now, "txn.decision", node=self.sequencer,
                          txn=f"batch-{batch['batch']}", decision="sequenced",
                          reason=f"{len(batch['txns'])} 个事务被定序并复制")
        for entry in batch["txns"]:
            self.committed.add(entry["txn"])
        for shard_id in self.shard_ids:
            self.network.send(self.sequencer, shard_id, batch,
                              label=f"批次 {batch['batch']}")

    def _sequencer_receive(self, source: str, message: dict[str, Any]) -> None:
        if message.get("type") == "BatchDone":
            self.batch_acks.setdefault(message["batch"], set()).add(source)

    # -- reporting ----------------------------------------------------------------

    def total(self) -> int:
        return sum(shard.balance for shard in self.shards.values())

    def snapshot(self) -> dict[str, Any]:
        result = {
            shard_id: {"alive": shard.alive, "state": "locked" if shard.lock else "idle",
                       "term": 0, "commit": 0, "base": 0, "log": [], "applied": [],
                       "machine": shard.summary()}
            for shard_id, shard in self.shards.items()
        }
        return result
