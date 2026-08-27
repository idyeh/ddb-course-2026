"""Stable sharding examples that do not depend on Python's randomized hash()."""

from __future__ import annotations

from collections import Counter
from hashlib import blake2b
from random import Random


def _score(text: str) -> int:
    digest = blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=False)


def modulo_bucket(key: str, bucket_count: int) -> int:
    if bucket_count <= 0:
        raise ValueError("bucket_count must be positive")
    return _score(key) % bucket_count


def rendezvous_node(key: str, nodes: list[str]) -> str:
    if not nodes:
        raise ValueError("nodes must not be empty")
    return max(nodes, key=lambda node: (_score(f"{key}\0{node}"), node))


def workload(seed: int, requests: int, hot_probability: float = 0.7) -> list[str]:
    rng = Random(seed)
    keys = []
    for _ in range(requests):
        if rng.random() < hot_probability:
            keys.append("tenant-hot")
        else:
            keys.append(f"tenant-{rng.randrange(1, 101)}")
    return keys


def node_load(seed: int, requests: int, nodes: list[str]) -> Counter[str]:
    return Counter(rendezvous_node(key, nodes) for key in workload(seed, requests))


if __name__ == "__main__":
    print(node_load(seed=2026, requests=1_000, nodes=["n1", "n2", "n3"]))

