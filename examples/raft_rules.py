"""Small, testable Raft rules—not a partial production implementation."""

from random import Random


def majority(cluster_size: int) -> int:
    if cluster_size <= 0:
        raise ValueError("cluster_size must be positive")
    return cluster_size // 2 + 1


def election_timeouts(seed: int, nodes: int, low: int = 150, high: int = 300) -> list[int]:
    if nodes <= 0 or low >= high:
        raise ValueError("invalid timeout configuration")
    rng = Random(seed)
    return [rng.randrange(low, high) for _ in range(nodes)]


def candidate_is_up_to_date(
    candidate_last_term: int,
    candidate_last_index: int,
    voter_last_term: int,
    voter_last_index: int,
) -> bool:
    candidate = (candidate_last_term, candidate_last_index)
    voter = (voter_last_term, voter_last_index)
    return candidate >= voter


def can_advance_commit(
    match_indexes: list[int],
    log_terms: list[int],
    candidate_index: int,
    current_term: int,
) -> bool:
    """Apply Raft's current-term restriction when advancing commitIndex.

    log_terms uses ordinary Python indexing, so log index N is stored at N - 1.
    match_indexes includes the leader's own replicated index.
    """
    if candidate_index <= 0 or candidate_index > len(log_terms):
        return False
    replicated = sum(index >= candidate_index for index in match_indexes)
    return (
        replicated >= majority(len(match_indexes))
        and log_terms[candidate_index - 1] == current_term
    )


if __name__ == "__main__":
    print(election_timeouts(seed=2026, nodes=5))
    print(can_advance_commit([4, 4, 3, 2, 1], [1, 1, 2, 3], 4, 3))

