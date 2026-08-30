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


def token(op_id: str, request_id: str, name: str, value: Any = None) -> dict[str, Any]:
    """Write a token. With `value=None` the replica invents one while applying,
    which is exactly the mistake the course warns about."""
    return {"kind": "token", "name": name, "value": value,
            "opId": op_id, "reqId": request_id}


def noop(term: int) -> dict[str, Any]:
    """A leader's no-op entry. Committing it in the current term is what lets the
    leader know its own commit position covers everything from earlier terms."""
    return {"kind": "noop", "opId": f"noop-t{term}", "reqId": f"noop-t{term}"}


def read(op_id: str, request_id: str, key: str) -> dict[str, Any]:
    return {"kind": "read", "key": key, "opId": op_id, "reqId": request_id}


def txn(op_id: str, request_id: str, writes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A cross-shard transaction: {participant: {key: value}}."""
    return {"kind": "txn", "writes": writes, "opId": op_id, "reqId": request_id}


def label(command: dict[str, Any]) -> str:
    if command["kind"] == "incr":
        return f"incr +{command['by']}"
    if command["kind"] == "set":
        return f"写 {command['key']}={command['value']}"
    if command["kind"] == "read":
        return f"读 {command['key']}"
    if command["kind"] == "noop":
        return "noop"
    if command["kind"] == "token":
        decided = command.get("value")
        return f"token {command['name']}" + (f"={decided}" if decided is not None else "（应用时生成）")
    if command["kind"] == "txn":
        pairs = [f"{key}={value}"
                 for keys in command["writes"].values() for key, value in sorted(keys.items())]
        return f"事务 {command['opId']}: " + ",".join(sorted(pairs))
    return command["kind"]


class StateMachine:
    def __init__(self, owner: str = "", clock: Any = None) -> None:
        # `owner` and `clock` exist only so a scenario can demonstrate what happens
        # when apply() reaches for something that differs between replicas.
        self.owner = owner
        self._clock = clock or (lambda: 0)
        self.reset()

    def reset(self) -> None:
        self.counter = 0
        self.kv: dict[str, Any] = {}
        self.results: dict[str, Any] = {}  # request_id -> result, replicated with the state
        self.applied: list[str] = []  # op ids in apply order, used by the checkers

    def apply(self, command: dict[str, Any]) -> tuple[Any, bool]:
        """Return (result, deduplicated). A repeated request_id never applies twice."""
        if command["kind"] == "noop":
            return None, False  # carries no effect and no dedup entry

        request_id = command["reqId"]
        if request_id in self.results:
            return self.results[request_id], True

        if command["kind"] == "incr":
            self.counter += command["by"]
            result = self.counter
        elif command["kind"] == "set":
            self.kv[command["key"]] = command["value"]
            result = command["value"]
        elif command["kind"] == "token":
            decided = command.get("value")
            # Nothing else in this file reads the clock or the node identity.
            result = decided if decided is not None else f"{self.owner}@{self._clock()}"
            self.kv[command["name"]] = result
        else:
            raise ValueError(f"unknown command {command['kind']!r}")

        self.results[request_id] = result
        self.applied.append(command["opId"])
        return result, False

    def capture(self) -> dict[str, Any]:
        """A snapshot must carry the dedup table too, not just the business data."""
        return {
            "counter": self.counter,
            "kv": dict(self.kv),
            "results": dict(self.results),
            "applied": list(self.applied),
        }

    def restore(self, data: dict[str, Any]) -> None:
        self.counter = data["counter"]
        self.kv = dict(data["kv"])
        self.results = dict(data["results"])
        self.applied = list(data["applied"])

    def summary(self) -> str:
        parts = []
        if self.counter or not self.kv:
            parts.append(f"counter={self.counter}")
        for key in sorted(self.kv):
            parts.append(f"{key}={self.kv[key]}")
        return " ".join(parts)
