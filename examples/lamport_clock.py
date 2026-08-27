"""Lamport clocks preserve causality without pretending to measure elapsed time."""

from dataclasses import dataclass


@dataclass
class Process:
    name: str
    clock: int = 0

    def local_event(self) -> int:
        self.clock += 1
        return self.clock

    def send(self) -> int:
        self.clock += 1
        return self.clock

    def receive(self, message_timestamp: int) -> int:
        self.clock = max(self.clock, message_timestamp) + 1
        return self.clock


def demonstration() -> tuple[int, int, int]:
    alice = Process("alice")
    bob = Process("bob")
    sent_at = alice.send()
    bob.local_event()
    received_at = bob.receive(sent_at)
    return sent_at, received_at, bob.clock


if __name__ == "__main__":
    print(demonstration())

