"""A brute-force linearizability checker for a small read/write register history."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Operation:
    identifier: str
    kind: str
    value: int | None
    result: int | None
    call: int
    returned: int


def is_linearizable(history: list[Operation], initial: int = 0) -> bool:
    if any(op.call > op.returned for op in history):
        raise ValueError("an operation cannot return before it is called")
    if any(op.kind not in {"read", "write"} for op in history):
        raise ValueError("kind must be read or write")

    predecessors = {
        op.identifier: {
            other.identifier
            for other in history
            if other.returned <= op.call and other.identifier != op.identifier
        }
        for op in history
    }

    def search(pending: tuple[Operation, ...], done: frozenset[str], state: int) -> bool:
        if not pending:
            return True
        for operation in pending:
            if not predecessors[operation.identifier] <= done:
                continue
            if operation.kind == "read" and operation.result != state:
                continue
            next_state = state if operation.kind == "read" else operation.value
            assert next_state is not None
            remaining = tuple(item for item in pending if item != operation)
            if search(remaining, done | {operation.identifier}, next_state):
                return True
        return False

    identifiers = [operation.identifier for operation in history]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("operation identifiers must be unique")
    return search(tuple(history), frozenset(), initial)


if __name__ == "__main__":
    sample = [
        Operation("w1", "write", 1, None, 0, 2),
        Operation("r1", "read", None, 1, 1, 3),
    ]
    print(is_linearizable(sample))

