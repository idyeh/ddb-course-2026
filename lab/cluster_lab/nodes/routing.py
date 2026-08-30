"""Sharding strategies and a routing table that can go stale.

Placement uses the stable hash from `examples/sharding.py`; Python's built-in
`hash()` is randomised per process and would make a run unreproducible.
"""

from __future__ import annotations

from typing import Any

from examples.sharding import _score, modulo_bucket

RANGE = "range"
HASH = "hash"
COMPOSITE = "composite"
SALTED = "salted"

STRATEGY_LABELS = {
    RANGE: "Range 分片：按键顺序切分",
    HASH: "Hash 分片：稳定散列后取模",
    COMPOSITE: "复合键：租户在前，同租户共置",
    SALTED: "加盐：热点键前缀打散，读侧再合并",
}


def tenant_of(key: str) -> str:
    return key.split("/", 1)[0]


def shard_for(key: str, strategy: str, shards: int, hot_tenant: str = "", salt: int = 4) -> int:
    """Map a key of the form `tenant/item` to a shard index."""
    if strategy == HASH:
        return modulo_bucket(key, shards)
    if strategy == RANGE:
        # Ordered key space cut into equal slices: neighbours stay together, and a
        # hot prefix therefore concentrates on one slice.
        tenant = tenant_of(key)
        number = int(tenant.split("-")[-1]) if "-" in tenant else 0
        return min(shards - 1, number * shards // 100)
    if strategy == COMPOSITE:
        return modulo_bucket(tenant_of(key), shards)
    if strategy == SALTED:
        tenant = tenant_of(key)
        if tenant == hot_tenant:
            # Only the hot tenant is spread; everything else keeps its locality.
            bucket = _score(key) % salt
            return modulo_bucket(f"{tenant}#{bucket}", shards)
        return modulo_bucket(tenant, shards)
    raise ValueError(f"unknown strategy {strategy!r}")


def scan_shards(strategy: str, shards: int, salt: int, hot: bool) -> int:
    """How many shards a single-tenant scan has to touch.

    This is the other half of the trade-off: spreading a hot key improves balance
    and costs locality, and it costs it only for the key that was spread.
    """
    if strategy in {RANGE, COMPOSITE}:
        return 1
    if strategy == SALTED:
        return min(shards, salt) if hot else 1
    return shards  # hashing every key individually keeps no locality at all


def workload(seed: int, requests: int, tenants: int, hot_share: float,
             hot_tenant: str) -> list[str]:
    """A skewed key stream, shaped like `examples/sharding.py`'s hot-tenant workload."""
    from random import Random

    rng = Random(seed)
    keys = []
    for number in range(requests):
        if rng.random() < hot_share:
            tenant = hot_tenant
        else:
            tenant = f"tenant-{rng.randrange(1, tenants + 1)}"
        keys.append(f"{tenant}/item-{number}")
    return keys


class RoutingTable:
    """What a client caches. It can be wrong, and being wrong must be recoverable."""

    def __init__(self, strategy: str, shards: int, hot_tenant: str = "") -> None:
        self.strategy = strategy
        self.shards = shards
        self.hot_tenant = hot_tenant
        self.version = 1
        self.overrides: dict[str, int] = {}

    def route(self, key: str) -> int:
        if key in self.overrides:
            return self.overrides[key]
        return shard_for(key, self.strategy, self.shards, self.hot_tenant)

    def split(self, moved: dict[str, int]) -> None:
        self.overrides.update(moved)
        self.version += 1

    def copy(self) -> "RoutingTable":
        clone = RoutingTable(self.strategy, self.shards, self.hot_tenant)
        clone.version = self.version
        clone.overrides = dict(self.overrides)
        return clone

    def snapshot(self) -> dict[str, Any]:
        return {"version": self.version, "overrides": dict(self.overrides)}


class ShardNode:
    """A shard that answers only for the keys it currently owns."""

    def __init__(self, node_id: str, system: "ShardSystem") -> None:
        self.id = node_id
        self.system = system
        self.data: dict[str, Any] = {}

    def summary(self) -> str:
        return " ".join(f"{key.split('/')[-1]}={value}"
                        for key, value in sorted(self.data.items())) or "空"

    def receive(self, source: str, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind not in {"ClientRequest", "ClientRead"}:
            return
        key = message["key"] if kind == "ClientRead" else message["command"]["key"]
        owner = self.system.owner_of(key)
        stale = owner != self.id

        if stale:
            self.system.trace.record(self.system.kernel.now, "shard.stale", node=self.id,
                                     key=key, owner=owner, policy=self.system.stale_policy)
            if self.system.stale_policy == REJECT_STALE:
                # A stale route is a recoverable protocol state, not a server error.
                self.system.network.send(self.id, source, {
                    "type": "ClientReply", "status": "redirect", "leader": owner,
                    "opId": message["opId"], "reqId": message["reqId"],
                }, label="RangeMoved")
                return

        if kind == "ClientRead":
            value = self.data.get(key)
            self.system.trace.record(self.system.kernel.now, "shard.route", key=key,
                                     shard=self.id, owner=owner, stale=stale,
                                     read=True, arriveAt=self.system.kernel.now)
            self.system.network.send(self.id, source, {
                "type": "ClientReply", "status": "ok", "result": value,
                "opId": message["opId"], "reqId": message["reqId"], "servedBy": self.id,
            }, label=f"{value}")
            return

        command = message["command"]
        self.data[key] = command["value"]
        self.system.trace.record(self.system.kernel.now, "shard.apply", key=key,
                                 shard=self.id, owner=owner, value=command["value"])
        self.system.trace.record(self.system.kernel.now, "shard.route", key=key,
                                 shard=self.id, owner=owner, stale=stale, read=False,
                                 arriveAt=self.system.kernel.now)
        self.system.network.send(self.id, source, {
            "type": "ClientReply", "status": "ok", "result": command["value"],
            "opId": message["opId"], "reqId": message["reqId"], "servedBy": self.id,
        }, label="ok")


REJECT_STALE = "reject"
ACCEPT_STALE = "accept"


class ShardSystem:
    def __init__(self, kernel: Any, trace: Any, network: Any, shard_ids: list[str],
                 strategy: str, hot_tenant: str = "", stale_policy: str = REJECT_STALE) -> None:
        self.kernel = kernel
        self.trace = trace
        self.network = network
        self.shard_ids = list(shard_ids)
        self.table = RoutingTable(strategy, len(shard_ids), hot_tenant)
        self.stale_policy = stale_policy
        self.nodes = {shard_id: ShardNode(shard_id, self) for shard_id in shard_ids}
        for shard_id, node in self.nodes.items():
            trace.add_node(shard_id, kind="shard")
            network.attach(shard_id, node.receive)

    def owner_of(self, key: str) -> str:
        return self.shard_ids[self.table.route(key)]

    def split(self, keys: list[str], destination: str) -> None:
        index = self.shard_ids.index(destination)
        moved = {}
        for key in keys:
            source = self.owner_of(key)
            if source == destination:
                continue
            moved[key] = index
            value = self.nodes[source].data.pop(key, None)
            if value is not None:
                self.nodes[destination].data[key] = value
        self.table.split(moved)
        self.trace.record(self.kernel.now, "shard.split", keys=sorted(moved),
                          to=destination, version=self.table.version)

    def snapshot(self) -> dict[str, Any]:
        return {
            shard_id: {"alive": True, "state": "shard", "term": 0, "commit": 0, "base": 0,
                       "log": [], "applied": [], "machine": node.summary()}
            for shard_id, node in self.nodes.items()
        }
