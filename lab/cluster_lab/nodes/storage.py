"""WAL, buffer pool and ARIES recovery: Analysis → Redo → Undo.

Two ordering rules decide whether the log can still explain the disk afterwards:

1. a log record describing a page update must reach disk before that page does;
2. a commit must be durable before the client is told it committed.

Both are switchable here, because a rule you can turn off is a rule a class can
watch break.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WAL = "wal"
NO_WAL = "no_wal"
FSYNC_THEN_ACK = "fsync_then_ack"
ACK_THEN_FSYNC = "ack_then_fsync"

UPDATE = "update"
COMMIT = "commit"
CHECKPOINT = "checkpoint"
CLR = "clr"

PAGES = {"P1": ["a", "c"], "P2": ["b", "d"]}
KEY_PAGE = {key: page for page, keys in PAGES.items() for key in keys}


@dataclass
class LogRecord:
    lsn: int
    kind: str
    txn: str = ""
    page: str = ""
    key: str = ""
    before: Any = None
    after: Any = None
    undo_next: int | None = None
    active: list[str] = field(default_factory=list)
    dirty: dict[str, int] = field(default_factory=dict)

    def label(self) -> str:
        if self.kind == UPDATE:
            return f"{self.txn} {self.key}:{self.before}→{self.after}"
        if self.kind == CLR:
            return f"CLR {self.txn} {self.key}←{self.after}"
        if self.kind == COMMIT:
            return f"{self.txn} commit"
        return "checkpoint"


class Storage:
    def __init__(self, kernel: Any, trace: Any, wal_policy: str = WAL,
                 commit_policy: str = FSYNC_THEN_ACK, write_clr: bool = True) -> None:
        self.kernel = kernel
        self.trace = trace
        self.wal_policy = wal_policy
        self.commit_policy = commit_policy
        self.write_clr = write_clr

        self.log: list[LogRecord] = []
        self.durable_lsn = 0
        self.buffer = {page: {"values": {key: 0 for key in keys}, "pageLSN": 0, "dirty": False}
                       for page, keys in PAGES.items()}
        self.disk = {page: {"values": {key: 0 for key in keys}, "pageLSN": 0}
                     for page, keys in PAGES.items()}
        self.acked: list[str] = []
        for page in PAGES:
            # Seed the view with the starting contents of every page.
            trace.record(kernel.now, "page.state", page=page,
                         values=dict(self.disk[page]["values"]), pageLSN=0)
        self.undo_steps = 0
        self.redo_skipped = 0
        self.redo_applied = 0

    # -- normal running -----------------------------------------------------------

    def _append(self, record: LogRecord) -> LogRecord:
        record.lsn = len(self.log) + 1
        self.log.append(record)
        self.trace.record(self.kernel.now, "wal.append", lsn=record.lsn, kind=record.kind,
                          txn=record.txn, page=record.page, key=record.key,
                          before=record.before, after=record.after,
                          undoNext=record.undo_next, label=record.label())
        return record

    def flush_log(self, upto: int | None = None) -> None:
        target = len(self.log) if upto is None else upto
        if target <= self.durable_lsn:
            return
        self.durable_lsn = target
        self.trace.record(self.kernel.now, "wal.flush", upto=self.durable_lsn)

    def update(self, txn: str, key: str, value: Any) -> None:
        page = KEY_PAGE[key]
        before = self.buffer[page]["values"][key]
        record = self._append(LogRecord(0, UPDATE, txn=txn, page=page, key=key,
                                        before=before, after=value))
        self.buffer[page]["values"][key] = value
        self.buffer[page]["pageLSN"] = record.lsn
        self.buffer[page]["dirty"] = True
        self.trace.record(self.kernel.now, "page.update", page=page, key=key, value=value,
                          pageLSN=record.lsn, location="buffer")

    def commit(self, txn: str) -> None:
        record = self._append(LogRecord(0, COMMIT, txn=txn))
        if self.commit_policy == FSYNC_THEN_ACK:
            self.flush_log(record.lsn)
        self.acked.append(txn)
        self.trace.record(self.kernel.now, "txn.acked", txn=txn, lsn=record.lsn,
                          durable=self.durable_lsn >= record.lsn)

    def checkpoint(self) -> None:
        active = sorted({record.txn for record in self.log if record.kind == UPDATE}
                        - {record.txn for record in self.log if record.kind == COMMIT})
        dirty = {page: self._rec_lsn(page) for page, state in self.buffer.items()
                 if state["dirty"]}
        record = self._append(LogRecord(0, CHECKPOINT, active=active, dirty=dirty))
        self.flush_log(record.lsn)

    def _rec_lsn(self, page: str) -> int:
        """Earliest log record that could still need redoing for this page."""
        since = self.disk[page]["pageLSN"]
        for record in self.log:
            if record.kind in {UPDATE, CLR} and record.page == page and record.lsn > since:
                return record.lsn
        return self.buffer[page]["pageLSN"]

    def flush_page(self, page: str) -> None:
        if self.wal_policy == WAL:
            # Rule 1: the log record describing this page must be durable first.
            self.flush_log(self.buffer[page]["pageLSN"])
        self.disk[page] = {"values": dict(self.buffer[page]["values"]),
                           "pageLSN": self.buffer[page]["pageLSN"]}
        self.buffer[page]["dirty"] = False
        self.trace.record(self.kernel.now, "page.flush", page=page,
                          pageLSN=self.disk[page]["pageLSN"],
                          values=dict(self.disk[page]["values"]),
                          durableLsn=self.durable_lsn)

    # -- crash --------------------------------------------------------------------

    def crash(self) -> None:
        lost = len(self.log) - self.durable_lsn
        self.log = self.log[: self.durable_lsn]
        self.buffer = {page: {"values": dict(self.disk[page]["values"]),
                              "pageLSN": self.disk[page]["pageLSN"], "dirty": False}
                       for page in PAGES}
        self.trace.record(self.kernel.now, "fault", kind="crash",
                          durableLsn=self.durable_lsn, lostRecords=lost)

    def expected(self) -> dict[str, Any]:
        """Ground truth: durable commits applied in full, everything else absent."""
        winners = {record.txn for record in self.log if record.kind == COMMIT}
        values = {key: 0 for key in KEY_PAGE}
        for record in self.log:
            if record.kind == UPDATE and record.txn in winners:
                values[record.key] = record.after
        return values

    def state(self) -> dict[str, Any]:
        values = {}
        for page, state in self.buffer.items():
            values.update(state["values"])
        return values

    # -- ARIES --------------------------------------------------------------------

    def recover(self, stop_after_undo: int | None = None) -> bool:
        """Return True when recovery finished; False when it was interrupted."""
        losers, dirty, start = self._analysis()
        self._redo(dirty, start)
        return self._undo(losers, stop_after_undo)

    def _analysis(self) -> tuple[dict[str, int | None], dict[str, int], int]:
        self.trace.record(self.kernel.now, "recovery.phase", phase="analysis")
        checkpoint = next((record for record in reversed(self.log)
                           if record.kind == CHECKPOINT), None)
        dirty = dict(checkpoint.dirty) if checkpoint else {}
        active = set(checkpoint.active) if checkpoint else set()
        begin = checkpoint.lsn if checkpoint else 0

        # A transaction that committed before the checkpoint is already a winner;
        # analysis lists it so the table shows what recovery believes.
        committed: set[str] = {record.txn for record in self.log
                               if record.kind == COMMIT and record.lsn <= begin}
        for record in self.log:
            self.trace.record(self.kernel.now, "recovery.scan", lsn=record.lsn)
            if record.lsn <= begin:
                continue
            if record.kind in {UPDATE, CLR}:
                active.add(record.txn)
                dirty.setdefault(record.page, record.lsn)
            elif record.kind == COMMIT:
                committed.add(record.txn)
                active.discard(record.txn)

        losers = {txn: None for txn in sorted(active - committed)}
        for txn in sorted(committed):
            self.trace.record(self.kernel.now, "recovery.txn", txn=txn, role="winner")
        for txn in losers:
            self.trace.record(self.kernel.now, "recovery.txn", txn=txn, role="loser")
        for page, rec_lsn in sorted(dirty.items()):
            self.trace.record(self.kernel.now, "recovery.dirty", page=page, recLSN=rec_lsn)
        start = min(dirty.values(), default=len(self.log) + 1)
        return losers, dirty, start

    def _redo(self, dirty: dict[str, int], start: int) -> None:
        self.trace.record(self.kernel.now, "recovery.phase", phase="redo")
        for record in self.log:
            if record.kind not in {UPDATE, CLR} or record.lsn < start:
                continue
            page = self.buffer[record.page]
            reason = ""
            if record.page not in dirty:
                reason = "页面不在脏页表中"
            elif record.lsn < dirty[record.page]:
                reason = "早于该页 recLSN"
            elif page["pageLSN"] >= record.lsn:
                reason = f"pageLSN {page['pageLSN']} ≥ 记录 LSN"
            if reason:
                self.redo_skipped += 1
                self.trace.record(self.kernel.now, "recovery.redo", lsn=record.lsn,
                                  applied=False, reason=reason)
                continue
            # Repeat history: losers' updates are replayed too, then undone.
            page["values"][record.key] = record.after
            page["pageLSN"] = record.lsn
            self.redo_applied += 1
            self.trace.record(self.kernel.now, "recovery.redo", lsn=record.lsn, applied=True,
                              key=record.key, value=record.after, reason="重演历史")

    def _undo(self, losers: dict[str, int | None], stop_after: int | None) -> bool:
        self.trace.record(self.kernel.now, "recovery.phase", phase="undo")
        resume = {}
        for txn in losers:
            # A CLR records how far this transaction has already been compensated.
            done = [record for record in self.log if record.kind == CLR and record.txn == txn]
            resume[txn] = done[-1].undo_next if done else None

        pending = [record for record in reversed(self.log)
                   if record.kind == UPDATE and record.txn in losers
                   and (resume[record.txn] is None or record.lsn <= resume[record.txn])]

        for record in pending:
            if stop_after is not None and self.undo_steps >= stop_after:
                self.trace.record(self.kernel.now, "fault", kind="crash_during_recovery",
                                  durableLsn=self.durable_lsn)
                return False
            page = self.buffer[record.page]
            page["values"][record.key] = record.before
            self.undo_steps += 1
            if self.write_clr:
                earlier = [item.lsn for item in self.log
                           if item.kind == UPDATE and item.txn == record.txn
                           and item.lsn < record.lsn]
                clr = self._append(LogRecord(0, CLR, txn=record.txn, page=record.page,
                                             key=record.key, before=record.after,
                                             after=record.before,
                                             undo_next=max(earlier) if earlier else 0))
                self.flush_log(clr.lsn)
                page["pageLSN"] = clr.lsn
            self.trace.record(self.kernel.now, "recovery.undo", lsn=record.lsn, txn=record.txn,
                              key=record.key, value=record.before, clr=self.write_clr)
        self.trace.record(self.kernel.now, "recovery.phase", phase="done")
        return True
