"""A tiny event-driven network simulator whose failures can be replayed."""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
from random import Random


@dataclass(order=True)
class Event:
    at: int
    sequence: int
    source: str = field(compare=False)
    target: str = field(compare=False)
    payload: str = field(compare=False)


class Network:
    def __init__(self, seed: int, drop_probability: float = 0.2) -> None:
        if not 0.0 <= drop_probability <= 1.0:
            raise ValueError("drop_probability must be between 0 and 1")
        self.rng = Random(seed)
        self.drop_probability = drop_probability
        self.now = 0
        self._sequence = 0
        self._events: list[Event] = []
        self.trace: list[tuple[object, ...]] = []

    def send(self, source: str, target: str, payload: str) -> None:
        if self.rng.random() < self.drop_probability:
            self.trace.append(("drop", self.now, source, target, payload))
            return
        delay = self.rng.randint(1, 5)
        self._sequence += 1
        event = Event(self.now + delay, self._sequence, source, target, payload)
        heapq.heappush(self._events, event)
        self.trace.append(("send", self.now, source, target, payload, delay))

    def run(self) -> list[tuple[object, ...]]:
        while self._events:
            event = heapq.heappop(self._events)
            self.now = event.at
            self.trace.append(
                ("deliver", self.now, event.source, event.target, event.payload)
            )
        return list(self.trace)


def scenario(seed: int) -> list[tuple[object, ...]]:
    network = Network(seed=seed)
    for index in range(6):
        network.send("client", "leader", f"request-{index}")
    return network.run()


if __name__ == "__main__":
    for record in scenario(seed=2026):
        print(record)

