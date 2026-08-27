"""Repeat history, then undo losers: a compact recovery teaching model."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Update:
    lsn: int
    transaction: str
    key: str
    before: int
    after: int


@dataclass(frozen=True)
class Commit:
    lsn: int
    transaction: str


Record = Update | Commit


def recover(checkpoint: dict[str, int], log: list[Record]) -> dict[str, int]:
    """Rebuild state from a checkpoint and a log with before/after images."""
    state = dict(checkpoint)
    committed = {record.transaction for record in log if isinstance(record, Commit)}

    # Redo repeats history, including updates by transactions that later need undo.
    for record in sorted(log, key=lambda item: item.lsn):
        if isinstance(record, Update):
            state[record.key] = record.after

    # Undo uncommitted transactions in reverse log order.
    for record in sorted(log, key=lambda item: item.lsn, reverse=True):
        if isinstance(record, Update) and record.transaction not in committed:
            state[record.key] = record.before
    return state


def demonstration() -> dict[str, int]:
    log: list[Record] = [
        Update(1, "T1", "x", 0, 10),
        Update(2, "T2", "y", 0, 20),
        Commit(3, "T1"),
    ]
    return recover({"x": 0, "y": 0}, log)


if __name__ == "__main__":
    print(demonstration())

