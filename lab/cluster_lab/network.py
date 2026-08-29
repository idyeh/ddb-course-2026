"""A network that can delay, drop, reorder and partition messages, reproducibly.

Delivery order follows the kernel's (tick, sequence) ordering, exactly like
`examples/deterministic_network.py`, so replays are byte-for-byte identical.
"""

from __future__ import annotations

from typing import Any, Callable

from .kernel import Kernel
from .trace import Trace


class Network:
    def __init__(
        self,
        kernel: Kernel,
        trace: Trace,
        delay_min: int = 4,
        delay_max: int = 18,
        drop: float = 0.0,
        duplicate: float = 0.0,
    ) -> None:
        if delay_min < 1 or delay_max < delay_min:
            raise ValueError("delays must satisfy 1 <= delay_min <= delay_max")
        self.kernel = kernel
        self.trace = trace
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.drop = drop
        self.duplicate = duplicate
        self.partitions: list[set[str]] = []
        self.censors: list[Callable[[str, str, dict[str, Any]], str | None]] = []
        self.down: set[str] = set()
        self.sent = 0
        self.dropped = 0
        self._message_id = 0
        self._inbox: dict[str, Callable[[str, dict[str, Any]], None]] = {}

    # -- wiring ------------------------------------------------------------------

    def attach(self, node_id: str, deliver: Callable[[str, dict[str, Any]], None]) -> None:
        self._inbox[node_id] = deliver

    # -- faults ------------------------------------------------------------------

    def partition(self, groups: list[list[str]]) -> None:
        self.partitions = [set(group) for group in groups]
        self.trace.record(self.kernel.now, "fault", kind="partition",
                          groups=[sorted(group) for group in self.partitions])

    def censor_once(self, cause: str, predicate: Callable[[str, str, dict[str, Any]], bool]) -> None:
        """Drop the next message matching `predicate` (used to lose a single reply)."""

        def censor(source: str, target: str, message: dict[str, Any]) -> str | None:
            if predicate(source, target, message):
                self.censors.remove(censor)
                return cause
            return None

        self.censors.append(censor)

    def heal(self) -> None:
        self.partitions = []
        self.trace.record(self.kernel.now, "fault", kind="heal")

    def group_of(self, node: str) -> set[str] | None:
        for group in self.partitions:
            if node in group:
                return group
        return None

    def reachable(self, source: str, target: str) -> bool:
        if source in self.down or target in self.down:
            return False
        source_group = self.group_of(source)
        target_group = self.group_of(target)
        # Anything left out of every group (clients, for instance) stays connected.
        if source_group is None or target_group is None:
            return True
        return source_group is target_group

    # -- transport ---------------------------------------------------------------

    def send(self, source: str, target: str, message: dict[str, Any], label: str = "") -> None:
        self._message_id += 1
        identifier = f"m{self._message_id}"
        self.sent += 1
        delay = self.kernel.rng.randint(self.delay_min, self.delay_max)
        eta = self.kernel.now + delay
        kind = message.get("type", "message")
        self.trace.record(self.kernel.now, "msg.send", id=identifier, **{"from": source},
                          to=target, kind=kind, label=label, eta=eta)

        # Decide loss now, but let the animation show the message travelling part of
        # the way before it disappears: students should see where it died.
        censored = None
        for censor in list(self.censors):
            censored = censor(source, target, message)
            if censored:
                break
        lost = censored is None and self.kernel.rng.random() < self.drop
        if censored or lost or not self.reachable(source, target):
            cause = censored or ("loss" if lost else "partition")
            self.kernel.schedule(max(1, delay // 2), lambda: self._drop(identifier, cause))
            return

        self.kernel.schedule(delay, lambda: self._deliver(identifier, source, target, message))
        if self.kernel.rng.random() < self.duplicate:
            extra = self.kernel.rng.randint(self.delay_min, self.delay_max)
            self._message_id += 1
            twin = f"m{self._message_id}"
            self.trace.record(self.kernel.now, "msg.send", id=twin, **{"from": source},
                              to=target, kind=kind, label=f"{label} (重复)",
                              eta=self.kernel.now + delay + extra)
            self.kernel.schedule(delay + extra,
                                 lambda: self._deliver(twin, source, target, message))

    def _drop(self, identifier: str, cause: str) -> None:
        self.dropped += 1
        self.trace.record(self.kernel.now, "msg.drop", id=identifier, cause=cause)

    def _deliver(self, identifier: str, source: str, target: str, message: dict[str, Any]) -> None:
        # A partition can form while the message is in flight; check again on arrival.
        if not self.reachable(source, target):
            self._drop(identifier, "partition")
            return
        self.trace.record(self.kernel.now, "msg.deliver", id=identifier)
        handler = self._inbox.get(target)
        if handler is not None:
            handler(source, message)
