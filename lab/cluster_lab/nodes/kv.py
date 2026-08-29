"""The replicated state machine, plus the deduplication table that belongs to it.

Commands are plain dicts so they can be replicated, traced and replayed without
any hidden state: nothing here calls `random()` or reads the clock, which is the
condition the course states for deterministic log replay.
"""

from __future__ import annotations

from typing import Any


def incr(op_id: str, request_id: str, by: int = 1) -> dict[str, Any]:
    return {"kind": "incr", "by": by, "opId": op_id, "reqId": request_id}


def put(op_id: str, request_id: str, key: str, value: Any) -> dict[str, Any]:
    return {"kind": "set", "key": key, "value": value, "opId": op_id, "reqId": request_id}


def label(command: dict[str, Any]) -> str:
    if command["kind"] == "incr":
        return f"incr +{command['by']}"
    if command["kind"] == "set":
        return f"{command['key']}={command['value']}"
    return command["kind"]


class StateMachine:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counter = 0
        self.kv: dict[str, Any] = {}
        self.results: dict[str, Any] = {}  # request_id -> result, replicated with the state
        self.applied: list[str] = []  # op ids in apply order, used by the checkers

    def apply(self, command: dict[str, Any]) -> tuple[Any, bool]:
        """Return (result, deduplicated). A repeated request_id never applies twice."""
        request_id = command["reqId"]
        if request_id in self.results:
            return self.results[request_id], True

        if command["kind"] == "incr":
            self.counter += command["by"]
            result = self.counter
        elif command["kind"] == "set":
            self.kv[command["key"]] = command["value"]
            result = command["value"]
        else:
            raise ValueError(f"unknown command {command['kind']!r}")

        self.results[request_id] = result
        self.applied.append(command["opId"])
        return result, False

    def summary(self) -> str:
        parts = []
        if self.counter or not self.kv:
            parts.append(f"counter={self.counter}")
        for key in sorted(self.kv):
            parts.append(f"{key}={self.kv[key]}")
        return " ".join(parts)
