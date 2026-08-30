"""A linearizability checker for register histories, including unknown results.

`examples/history_checker.py` on the slides checks the easy case: every operation
returned. A real history also contains timeouts, and a timed-out operation may or
may not have taken effect. This checker models that directly:

- `fail` operations are removed; they definitely did not happen.
- `info` reads are removed; they observed nothing.
- `info` writes are optional: they may linearize anywhere after their call, or
  never at all. That is why the search succeeds as soon as every operation that
  actually returned has been placed.

The search is exhaustive, so it is exponential. `MAX_OPERATIONS` keeps a classroom
run bounded, and the UI says so rather than pretending the history was checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_OPERATIONS = 14


@dataclass(frozen=True)
class Op:
    id: str
    kind: str  # "read" | "write"
    value: Any
    call: int
    ret: int | None  # None means the operation never returned (unknown)
    process: str = ""

    def describe(self) -> str:
        when = f"[{self.call}, {self.ret}]" if self.ret is not None else f"[{self.call}, ?]"
        if self.kind == "write":
            return f"{self.id} 写 {self.value} {when}" + ("" if self.ret is not None else " 未知")
        return f"{self.id} 读 → {self.value} {when}"


def find_linearization(ops: list[Op], initial: Any = None) -> list[Op] | None:
    """Return one legal order, or None when the history admits none."""
    memo: set[tuple[tuple[Op, ...], Any]] = set()

    def search(pending: tuple[Op, ...], state: Any) -> list[Op] | None:
        if all(op.ret is None for op in pending):
            return []  # only unknown operations left: they simply never happened
        key = (pending, state)
        if key in memo:
            return None
        for op in pending:
            # Real time: anything that returned before this operation was called
            # has to be placed first.
            if any(other.ret is not None and other.ret <= op.call
                   for other in pending if other is not op):
                continue
            if op.kind == "read" and op.value != state:
                continue
            rest = tuple(item for item in pending if item is not op)
            tail = search(rest, state if op.kind == "read" else op.value)
            if tail is not None:
                return [op, *tail]
        memo.add(key)
        return None

    return search(tuple(ops), initial)


def is_linearizable(ops: list[Op], initial: Any = None) -> bool:
    return find_linearization(ops, initial) is not None


def _values_are_explained(ops: list[Op], initial: Any) -> bool:
    """Every surviving read must still have a write that could have produced it."""
    available = {initial} | {op.value for op in ops if op.kind == "write"}
    return all(op.value in available for op in ops if op.kind == "read")


def shrink(ops: list[Op], initial: Any = None) -> list[Op]:
    """Drop operations that are not needed to keep the history illegal.

    Deleting a read only ever removes a constraint, so it is always safe. Deleting
    a write removes a state transition and can make a legal history look illegal --
    which would hand the class a counterexample that proves nothing. Writes are
    therefore dropped only when every remaining read still has a source for its
    value.
    """
    minimal = [op for op in ops]
    for candidate in [op for op in minimal if op.kind == "read"]:
        reduced = [op for op in minimal if op is not candidate]
        if any(op.kind == "read" for op in reduced) and not is_linearizable(reduced, initial):
            minimal = reduced
    for candidate in [op for op in minimal if op.kind == "write"]:
        reduced = [op for op in minimal if op is not candidate]
        if _values_are_explained(reduced, initial) and not is_linearizable(reduced, initial):
            minimal = reduced
    return minimal


def operations_from(events: list[dict[str, Any]]) -> dict[str, list[Op]]:
    """Group a recorded client history into one register history per key."""
    calls: dict[str, dict[str, Any]] = {}
    per_key: dict[str, list[Op]] = {}
    for event in events:
        if event["type"] == "client.call":
            calls[event["reqId"]] = event
        elif event["type"] == "client.return":
            call = calls.get(event["reqId"])
            if call is None or not call.get("key"):
                continue
            if event["status"] == "fail":
                continue
            kind = call.get("kind", "set")
            if kind == "read":
                if event["status"] != "ok":
                    continue  # a timed-out read observed nothing
                value = event.get("result")
            elif kind == "set":
                value = call.get("value")
            else:
                continue
            per_key.setdefault(call["key"], []).append(Op(
                id=call["opId"],
                kind="read" if kind == "read" else "write",
                value=value,
                call=call["t"],
                ret=event["t"] if event["status"] == "ok" else None,
                process=call.get("client", ""),
            ))
    return per_key
