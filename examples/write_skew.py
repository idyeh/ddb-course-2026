"""A minimal write-skew history and one way to make its conflict visible."""

from dataclasses import dataclass


@dataclass
class Hospital:
    alice_on_call: bool = True
    bob_on_call: bool = True
    guard_version: int = 0

    @property
    def on_call_count(self) -> int:
        return int(self.alice_on_call) + int(self.bob_on_call)


def under_snapshot_isolation() -> Hospital:
    state = Hospital()

    # Both transactions read the same initial snapshot and update different rows.
    alice_saw_bob = state.bob_on_call
    bob_saw_alice = state.alice_on_call
    if alice_saw_bob:
        state.alice_on_call = False
    if bob_saw_alice:
        state.bob_on_call = False
    return state


def with_shared_guard() -> tuple[Hospital, tuple[bool, bool]]:
    state = Hospital()
    alice_snapshot_version = state.guard_version
    bob_snapshot_version = state.guard_version

    alice_committed = alice_snapshot_version == state.guard_version
    if alice_committed:
        state.alice_on_call = False
        state.guard_version += 1

    bob_committed = bob_snapshot_version == state.guard_version
    if bob_committed:
        state.bob_on_call = False
        state.guard_version += 1

    return state, (alice_committed, bob_committed)


if __name__ == "__main__":
    print("snapshot isolation:", under_snapshot_isolation())
    print("shared guard:", with_shared_guard())

