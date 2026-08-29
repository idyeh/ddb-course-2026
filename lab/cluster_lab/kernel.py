"""A deterministic discrete-event kernel: logical ticks, one RNG, no wall clock.

Every scenario in the lab runs on top of this class.  Time is an integer tick
counter, never `time.time()`, and every random decision comes from `self.rng`,
so a seed is enough to replay a run exactly.
"""

from __future__ import annotations

import heapq
from random import Random
from typing import Callable


class Kernel:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.rng = Random(seed)
        self.now = 0
        self._sequence = 0
        self._queue: list[tuple[int, int, Callable[[], None]]] = []

    def schedule(self, delay: int, action: Callable[[], None]) -> int:
        """Run `action` after `delay` ticks; ties break on insertion order."""
        if delay < 0:
            raise ValueError("delay must not be negative")
        return self.schedule_at(self.now + delay, action)

    def schedule_at(self, when: int, action: Callable[[], None]) -> int:
        if when < self.now:
            raise ValueError("cannot schedule into the past")
        self._sequence += 1
        heapq.heappush(self._queue, (when, self._sequence, action))
        return when

    def run(self, until: int) -> None:
        """Drain the queue up to and including tick `until`."""
        while self._queue and self._queue[0][0] <= until:
            when, _, action = heapq.heappop(self._queue)
            self.now = when
            action()
        self.now = until

    @property
    def pending(self) -> int:
        return len(self._queue)
