"""A tiny distributed query executor: placement, exchange, join and aggregation.

The tables are small and the join actually runs, so a plan that moves the wrong
data does not merely look expensive here -- it returns the wrong rows. That is
the difference between a cost demonstration and a correctness one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from random import Random
from typing import Any

from examples.sharding import modulo_bucket

LOCAL = "local"
BROADCAST = "broadcast"
SHUFFLE = "shuffle"

ROW_BYTES = 512


@dataclass
class Row:
    row_id: str
    key: str
    tier: str
    amount: int


@dataclass
class Plan:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)

    def add(self, node_id: str, kind: str, label: str, layer: int, **fields: Any) -> str:
        self.nodes.append({"id": node_id, "kind": kind, "label": label, "layer": layer, **fields})
        return node_id

    def link(self, source: str, target: str, rows: int, kind: str = "pipe") -> None:
        self.edges.append({"from": source, "to": target, "rows": rows,
                           "bytes": rows * ROW_BYTES, "kind": kind})


def make_rows(seed: int, count: int, keys: int, hot_share: float, prefix: str) -> list[Row]:
    rng = Random(seed)
    rows = []
    for number in range(count):
        key = "k-hot" if rng.random() < hot_share else f"k-{rng.randrange(1, keys + 1)}"
        rows.append(Row(f"{prefix}{number}", key,
                        "gold" if rng.random() < 0.25 else "silver",
                        rng.randrange(10, 500)))
    return rows


def place(rows: list[Row], workers: int, by_key: bool) -> list[list[Row]]:
    """Partition rows across workers, either on the join key or on the row id."""
    buckets: list[list[Row]] = [[] for _ in range(workers)]
    for row in rows:
        target = modulo_bucket(row.key if by_key else row.row_id, workers)
        buckets[target].append(row)
    return buckets


def join_pairs(left: list[Row], right: list[Row]) -> int:
    index = Counter(row.key for row in right)
    return sum(index[row.key] for row in left)


@dataclass
class JoinResult:
    plan: Plan
    rows_out: int
    truth: int
    network_bytes: int
    worker_rows: list[int]
    peak_worker_bytes: int
    elapsed: int
    small_side_bytes: int


def run_join(strategy: str, left: list[Row], right: list[Row], workers: int,
             colocated: bool, bandwidth: int, cpu_per_row: int,
             startup: int) -> JoinResult:
    truth = join_pairs(left, right)
    left_parts = place(left, workers, by_key=colocated)
    right_parts = place(right, workers, by_key=colocated)
    plan = Plan()

    for worker in range(workers):
        plan.add(f"scanL{worker}", "scan", f"Scan 左表 w{worker}", 0,
                 worker=worker, rows=len(left_parts[worker]))
        plan.add(f"scanR{worker}", "scan", f"Scan 右表 w{worker}", 0,
                 worker=worker, rows=len(right_parts[worker]))

    network_rows = 0
    if strategy == LOCAL:
        exchange = plan.add("exchange", "exchange", "无交换 (colocated)", 1, rows=0)
        join_left, join_right = left_parts, right_parts
    elif strategy == BROADCAST:
        network_rows = len(right) * (workers - 1)
        exchange = plan.add("exchange", "exchange",
                            f"Broadcast 右表 ×{workers - 1}", 1, rows=network_rows)
        join_left = left_parts
        join_right = [list(right) for _ in range(workers)]
    else:
        moved_left = sum(1 for worker in range(workers) for row in left_parts[worker]
                         if modulo_bucket(row.key, workers) != worker)
        moved_right = sum(1 for worker in range(workers) for row in right_parts[worker]
                          if modulo_bucket(row.key, workers) != worker)
        network_rows = moved_left + moved_right
        exchange = plan.add("exchange", "exchange", "Repartition 双侧按连接键", 1,
                            rows=network_rows)
        join_left = place(left, workers, by_key=True)
        join_right = place(right, workers, by_key=True)

    for worker in range(workers):
        plan.link(f"scanL{worker}", exchange, len(left_parts[worker]))
        plan.link(f"scanR{worker}", exchange, len(right_parts[worker]))

    produced_by = []
    worker_rows = []
    for worker in range(workers):
        produced = join_pairs(join_left[worker], join_right[worker])
        inputs = len(join_left[worker]) + len(join_right[worker])
        produced_by.append(produced)
        worker_rows.append(inputs)
        plan.add(f"join{worker}", "join", f"Join w{worker}", 2,
                 worker=worker, rows=produced, inputs=inputs)
        plan.link(exchange, f"join{worker}", inputs, kind="exchange")

    rows_out = sum(produced_by)
    gather = plan.add("gather", "exchange", "Gather 汇总", 3, rows=rows_out)
    for worker in range(workers):
        plan.link(f"join{worker}", gather, produced_by[worker])
    result = plan.add("result", "result", "结果", 4, rows=rows_out)
    plan.link(gather, result, rows_out)

    peak = max(worker_rows) * ROW_BYTES if worker_rows else 0
    elapsed = startup + max(
        (rows * ROW_BYTES) // bandwidth + rows * cpu_per_row for rows in worker_rows
    ) if worker_rows else startup
    return JoinResult(plan, rows_out, truth, network_rows * ROW_BYTES, worker_rows,
                      peak, elapsed, len(right) * ROW_BYTES)


NONE = "none"
PREDICATE = "predicate"
PREDICATE_PROJECTION = "predicate_projection"
FULL = "full"

SUM = "sum"
AVG_NAIVE = "avg_naive"
AVG_DECOMPOSED = "avg_decomposed"

PROJECTED_BYTES = ROW_BYTES // 4
STATE_BYTES = 32


@dataclass
class AggregateResult:
    plan: Plan
    value: float
    truth: float
    scanned: int
    network_rows: int
    network_bytes: int
    peak_intermediate: int
    elapsed: int


def run_aggregation(pushdown: str, aggregate: str, rows: list[Row], workers: int,
                    bandwidth: int, cpu_per_row: int, startup: int) -> AggregateResult:
    parts = place(rows, workers, by_key=False)
    matching = [row for row in rows if row.tier == "gold"]
    truth = (sum(row.amount for row in matching) if aggregate == SUM
             else (sum(row.amount for row in matching) / len(matching) if matching else 0.0))

    plan = Plan()
    for worker in range(workers):
        plan.add(f"scan{worker}", "scan", f"Scan w{worker}", 0,
                 worker=worker, rows=len(parts[worker]))

    per_worker_rows = []
    per_worker_bytes = []
    partials = []
    for worker in range(workers):
        local = parts[worker]
        kept = local
        row_bytes = ROW_BYTES
        layer_id = f"scan{worker}"

        if pushdown in {PREDICATE, PREDICATE_PROJECTION, FULL}:
            kept = [row for row in local if row.tier == "gold"]
            layer_id = plan.add(f"filter{worker}", "filter", f"Filter w{worker}", 1,
                                worker=worker, rows=len(kept))
            plan.link(f"scan{worker}", layer_id, len(local))
        if pushdown in {PREDICATE_PROJECTION, FULL}:
            row_bytes = PROJECTED_BYTES
            projected = plan.add(f"project{worker}", "project", f"Project w{worker}", 2,
                                 worker=worker, rows=len(kept))
            plan.link(layer_id, projected, len(kept))
            layer_id = projected

        if pushdown == FULL:
            if aggregate == AVG_NAIVE:
                partial = (sum(row.amount for row in kept) / len(kept)) if kept else None
            elif aggregate == AVG_DECOMPOSED:
                partial = (sum(row.amount for row in kept), len(kept))
            else:
                partial = sum(row.amount for row in kept)
            partials.append(partial)
            shipped_rows, shipped_bytes = 1, STATE_BYTES
            layer_id = plan.add(f"partial{worker}", "aggregate", f"局部聚合 w{worker}", 3,
                                worker=worker, rows=1)
            plan.link(f"project{worker}", layer_id, len(kept))
        else:
            partials.append(kept)
            shipped_rows, shipped_bytes = len(kept), len(kept) * row_bytes

        per_worker_rows.append(shipped_rows)
        per_worker_bytes.append(shipped_bytes)
        plan.link(layer_id, "gather", shipped_rows, kind="exchange")

    gather = plan.add("gather", "exchange", "Gather 汇总", 4, rows=sum(per_worker_rows))
    final = plan.add("final", "aggregate", "最终聚合", 5, rows=1)
    plan.link(gather, final, sum(per_worker_rows))

    if pushdown == FULL:
        if aggregate == SUM:
            value = float(sum(partials))
        elif aggregate == AVG_NAIVE:
            # Averaging averages: only correct when every partition has the same size.
            seen = [item for item in partials if item is not None]
            value = sum(seen) / len(seen) if seen else 0.0
        else:
            totals = sum(item[0] for item in partials)
            counts = sum(item[1] for item in partials)
            value = totals / counts if counts else 0.0
    else:
        collected = [row for part in partials for row in part
                     if pushdown != NONE or row.tier == "gold"]
        value = (float(sum(row.amount for row in collected)) if aggregate == SUM
                 else (sum(row.amount for row in collected) / len(collected)
                       if collected else 0.0))

    elapsed = startup + max(
        per_worker_bytes[index] // bandwidth + len(parts[index]) * cpu_per_row
        for index in range(workers)
    )
    return AggregateResult(plan, round(value, 4), round(truth, 4), len(rows),
                           sum(per_worker_rows), sum(per_worker_bytes),
                           sum(per_worker_rows), elapsed)
