"""Invariant checkers. Every one of them reads the trace, not the simulator state.

That is the discipline the course asks for: a conclusion has to be derivable from
the recorded history, so the same evidence supports the animation, the test and
the report.
"""

from __future__ import annotations

from typing import Any

from examples.raft_rules import majority

from .checkers.linearizable import MAX_OPERATIONS, find_linearization, operations_from, shrink
from .registry import CheckResult
from .trace import Trace


def _events(trace: Trace, *kinds: str) -> list[dict[str, Any]]:
    wanted = set(kinds)
    return [event for event in trace.events if event["type"] in wanted]


def single_leader_per_term(trace: Trace) -> CheckResult:
    """Raft's election safety: one term elects at most one leader."""
    by_term: dict[int, set[str]] = {}
    for event in _events(trace, "node.state"):
        if event.get("state") == "leader":
            by_term.setdefault(event["term"], set()).add(event["node"])
    clashes = {term: nodes for term, nodes in by_term.items() if len(nodes) > 1}
    if clashes:
        term, nodes = next(iter(clashes.items()))
        return CheckResult(
            "每个任期至多一个 Leader (election safety)", False,
            f"任期 {term} 出现了 {len(nodes)} 个 Leader：{'、'.join(sorted(nodes))}",
            [event for event in _events(trace, "node.state")
             if event.get("state") == "leader" and event["term"] == term],
        )
    return CheckResult("每个任期至多一个 Leader (election safety)", True,
                       f"共完成 {len(by_term)} 次当选，无冲突。")


def committed_prefix_stable(trace: Trace) -> CheckResult:
    """A committed entry is never replaced by a different entry at the same index."""
    committed: dict[int, dict[str, Any]] = {}
    for event in trace.events:
        if event["type"] != "log.commit":
            continue
        known = committed.setdefault(event["index"], event)
        # An uncommitted append on a doomed minority is not a violation; it will be
        # truncated. A *second commit* of a different entry at the same index is.
        if (known["term"], known["cmd"]) != (event["term"], event["cmd"]):
            return CheckResult(
                "已提交前缀不被推翻 (committed prefix stable)", False,
                f"索引 {event['index']} 先被提交为 term {known['term']} / {known['cmd']}，"
                f"又在 t={event['t']} 被 {event['node']} 提交为 "
                f"term {event['term']} / {event['cmd']}",
                [known, event],
            )

    final = trace.facts.get("final", {})
    if final and committed:
        size = len(final)
        for index, known in sorted(committed.items()):
            # An entry may have been compacted away: a snapshot only ever covers
            # applied entries, so a node whose snapshot reaches this index still
            # holds it.
            survivors = sum(
                1 for state in final.values()
                if state.get("base", 0) >= index
                or any(entry["index"] == index and entry["term"] == known["term"]
                       for entry in state["log"])
            )
            if survivors < majority(size):
                return CheckResult(
                    "已提交前缀不被推翻 (committed prefix stable)", False,
                    f"索引 {index}（term {known['term']}，{known['cmd']}）已提交，"
                    f"但运行结束时只有 {survivors}/{size} 个副本仍持有它。",
                    [known],
                )
    return CheckResult("已提交前缀不被推翻 (committed prefix stable)", True,
                       f"共 {len(committed)} 条已提交条目，全部稳定。")


def acked_not_lost(trace: Trace) -> CheckResult:
    """Anything answered `ok` must still be applied on a majority at the end."""
    # Only writes leave a trace in the state machine; a read cannot be "lost".
    acked = [event for event in _events(trace, "client.return")
             if event["status"] == "ok" and event.get("kind") != "read"]
    final = trace.facts.get("final", {})
    if not final:
        return CheckResult("已确认写不丢失 (acked writes survive)", True, "无最终状态可检查。")
    size = len(final)
    for event in acked:
        holders = [node for node, state in final.items() if event["opId"] in state["applied"]]
        if len(holders) < majority(size):
            return CheckResult(
                "已确认写不丢失 (acked writes survive)", False,
                f"操作 {event['opId']} 在 t={event['t']} 被确认为 ok，"
                f"但结束时只有 {len(holders)}/{size} 个副本应用了它"
                + ("（该确认发生在复制之前）。" if event.get("ackedLocally") else "。"),
                [event],
            )
    return CheckResult("已确认写不丢失 (acked writes survive)", True,
                       f"{len(acked)} 个被确认的写全部保留在多数派上。")


def at_most_once(trace: Trace) -> CheckResult:
    """One logical operation takes effect at most once on each replica."""
    counts: dict[str, dict[str, int]] = {}
    for event in trace.events:
        if event["type"] == "node.crash":
            counts[event["node"]] = {}  # the state machine is rebuilt after a restart
        elif event["type"] == "state.apply" and not event.get("deduped"):
            per_node = counts.setdefault(event["node"], {})
            per_node[event["opId"]] = per_node.get(event["opId"], 0) + 1
            if per_node[event["opId"]] > 1:
                return CheckResult(
                    "同一操作至多生效一次 (at most once)", False,
                    f"操作 {event['opId']} 在 {event['node']} 上生效了 "
                    f"{per_node[event['opId']]} 次（不同的 request_id 无法被去重表识别）。",
                    [item for item in _events(trace, "state.apply")
                     if item["opId"] == event["opId"] and item["node"] == event["node"]],
                )
    total = sum(len(per_node) for per_node in counts.values())
    return CheckResult("同一操作至多生效一次 (at most once)", True,
                       f"检查了 {total} 组 (节点, 操作) 应用记录。")


def replicas_agree(trace: Trace) -> CheckResult:
    """Same log prefix plus deterministic commands must give the same state."""
    final = trace.facts.get("final", {})
    alive = {node: state for node, state in final.items() if state.get("alive")}
    machines = {state["machine"] for state in alive.values()}
    committed = {state["commit"] for state in alive.values()}
    if len(machines) > 1 and len(committed) == 1:
        return CheckResult(
            "相同日志得到相同状态 (deterministic apply)", False,
            "各副本提交位置相同，状态机内容却不同：" +
            "，".join(f"{node}={state['machine']}" for node, state in sorted(alive.items())),
            [],
        )
    return CheckResult("相同日志得到相同状态 (deterministic apply)", True,
                       "存活副本在相同提交位置上状态一致。")


def linearizable(trace: Trace) -> CheckResult:
    """Every operation must look instantaneous, and real-time order must hold."""
    name = "线性一致 (linearizable)"
    per_key = operations_from(trace.events)
    if not per_key:
        return CheckResult(name, True, "该运行没有可检查的单键读写历史。")

    checked = []
    for key, ops in sorted(per_key.items()):
        if len(ops) > MAX_OPERATIONS:
            return CheckResult.inconclusive(
                name,
                f"键 {key} 的历史有 {len(ops)} 个操作，超过穷举检查器的上限 "
                f"{MAX_OPERATIONS}，本次没有做线性一致检查。"
                "未检查既不是通过也不是违反：这条性质仍然待证。",
            )
        if find_linearization(ops) is not None:
            checked.append(f"{key}({len(ops)})")
            continue

        # Linearizability is composable per object, so one key is enough to refute.
        minimal = shrink(ops)
        by_id = {op.id: op for op in minimal}
        evidence = [
            event for event in trace.events
            if event["type"] in {"client.call", "client.return"} and event.get("opId") in by_id
        ]
        return CheckResult(
            name, False,
            f"键 {key} 的历史找不到合法的瞬时生效顺序。最小反例："
            + "；".join(op.describe() for op in minimal),
            evidence,
        )
    return CheckResult(name, True, "已检查 " + "、".join(checked) + "，存在合法的生效顺序。")


def atomic_commit(trace: Trace) -> CheckResult:
    """Every participant of a transaction must reach the same outcome."""
    name = "原子提交 (atomic commit)"
    outcomes: dict[str, dict[str, str]] = {}
    for event in _events(trace, "txn.state"):
        outcomes.setdefault(event["txn"], {})[event["node"]] = event["state"]
    for txn, per_node in sorted(outcomes.items()):
        if len(set(per_node.values())) > 1:
            split = "，".join(f"{node}={state}" for node, state in sorted(per_node.items()))
            return CheckResult(
                name, False, f"事务 {txn} 的参与者结果不一致：{split}",
                [event for event in _events(trace, "txn.state") if event["txn"] == txn],
            )
    if not outcomes:
        return CheckResult(name, True, "本次运行没有事务达成结果。")
    return CheckResult(name, True, f"{len(outcomes)} 个事务的参与者结果一致。")


def no_unilateral_resolution(trace: Trace) -> CheckResult:
    """A prepared participant may never resolve without a decision existing."""
    name = "prepared 参与者不擅自决定 (no unilateral resolution)"
    decided: dict[str, int] = {}
    for event in trace.events:
        if event["type"] == "txn.decision":
            decided.setdefault(event["txn"], event["t"])
        elif event["type"] == "txn.state":
            when = decided.get(event["txn"])
            if when is None or when > event["t"]:
                return CheckResult(
                    name, False,
                    f"{event['node']} 在 t={event['t']} 把事务 {event['txn']} 解到 "
                    f"{event['state']}，但此时还没有任何持久化决议。",
                    [event],
                )
    return CheckResult(name, True, "每个参与者的结果都来自一份已持久化的决议。")


def load_balance(trace: Trace) -> CheckResult:
    """A stated, checkable balance target -- not a vague wish for "even" load."""
    name = "分片负载均衡 (load balance)"
    load = trace.facts.get("shard_load") or {}
    threshold = trace.facts.get("balance_threshold", 2.5)
    if not load:
        return CheckResult(name, True, "本次运行没有分片负载数据。")
    counts = list(load.values())
    average = sum(counts) / len(counts)
    hottest = max(load, key=lambda shard: load[shard])
    ratio = load[hottest] / average if average else 0
    detail = (f"最热分片 {hottest} 承担 {load[hottest]} 个请求，平均 {average:.1f}，"
              f"比值 {ratio:.2f}（阈值 {threshold}）")
    return CheckResult(name, ratio <= threshold, detail)


def stable_routing(trace: Trace) -> CheckResult:
    """The same key must map to the same shard for the whole run."""
    name = "路由稳定 (stable routing)"
    seen: dict[str, str] = {}
    for event in _events(trace, "shard.route"):
        key, shard = event["key"], event["shard"]
        if key in seen and seen[key] != shard:
            return CheckResult(name, False,
                               f"键 {key} 先被路由到 {seen[key]}，又被路由到 {shard}", [event])
        seen[key] = shard
    return CheckResult(name, True, f"{len(seen)} 个键的映射在整个运行中保持不变。")


def routed_correctly(trace: Trace) -> CheckResult:
    """A write must be applied by the shard that actually owns the key."""
    name = "请求落到正确分片 (routed to the owner)"
    for event in _events(trace, "shard.apply"):
        if event["shard"] != event["owner"]:
            return CheckResult(
                name, False,
                f"键 {event['key']} 的写入被 {event['shard']} 执行，"
                f"但该键此时属于 {event['owner']}——过期的路由被当成了有效路由。",
                [event],
            )
    applied = len(_events(trace, "shard.apply"))
    return CheckResult(name, True, f"{applied} 次写入全部由键的所有者执行。")


def recovery_correct(trace: Trace) -> CheckResult:
    """After recovery: every winner's effect present, every loser's effect gone."""
    name = "恢复结果正确 (winners in, losers out)"
    expected = trace.facts.get("expected")
    recovered = trace.facts.get("recovered")
    if expected is None or recovered is None:
        return CheckResult(name, True, "本次运行没有恢复结果可检查。")
    wrong = {key: (recovered.get(key), value) for key, value in expected.items()
             if recovered.get(key) != value}
    if wrong:
        detail = "，".join(f"{key} 恢复为 {got}，应为 {want}"
                          for key, (got, want) in sorted(wrong.items()))
        return CheckResult(name, False, detail + "。日志已经无法解释磁盘上的内容。", [])
    return CheckResult(name, True,
                       "恢复后的状态与“已持久化提交的事务全部生效、未提交事务毫无痕迹”一致。")


def durable_commits(trace: Trace) -> CheckResult:
    """Anything the client was told committed must survive the crash."""
    name = "已确认提交不丢失 (durable commits)"
    acked = trace.facts.get("acked_commits") or []
    survived = set(trace.facts.get("surviving_commits") or [])
    lost = [txn for txn in acked if txn not in survived]
    if lost:
        return CheckResult(
            name, False,
            f"{'、'.join(lost)} 已经向客户端报告提交成功，崩溃后却没有留下任何痕迹："
            "提交记录在被通知之前没有到达承诺的持久化点。",
            [],
        )
    return CheckResult(name, True, f"{len(acked)} 笔已确认的提交全部在恢复后存在。")


def join_complete(trace: Trace) -> CheckResult:
    """The plan has to return every row the join actually has."""
    name = "Join 结果完整 (no lost rows)"
    produced = trace.facts.get("join_rows")
    truth = trace.facts.get("join_truth")
    if produced is None or truth is None:
        return CheckResult(name, True, "本次运行没有 Join 结果可检查。")
    if produced != truth:
        return CheckResult(
            name, False,
            f"计划返回 {produced} 行，实际应有 {truth} 行，少了 {truth - produced} 行。"
            "计划名称与执行状态都正常——错误只体现在结果里。",
        )
    return CheckResult(name, True, f"{produced} 行，与全量连接一致。")


def broadcast_fits_memory(trace: Trace) -> CheckResult:
    """Choosing broadcast without bounding the small side is a listed anti-pattern."""
    name = "广播端放得进内存 (broadcast fits)"
    needed = trace.facts.get("needs_broadcast_memory") or 0
    limit = trace.facts.get("memory_limit_bytes") or 0
    if not needed:
        return CheckResult(name, True, "本计划不需要在每个节点上放下整张表。")
    if needed > limit:
        return CheckResult(
            name, False,
            f"被广播的一侧需要 {needed / 1_000_000:.2f} MB，每节点上限 "
            f"{limit / 1_000_000:.2f} MB。估错“小表”会把内存与网络压力一起放大。",
        )
    return CheckResult(name, True,
                       f"被广播的一侧 {needed / 1_000_000:.2f} MB，在 "
                       f"{limit / 1_000_000:.2f} MB 上限内。")


def aggregate_result_correct(trace: Trace) -> CheckResult:
    """Pushing an aggregate down is only safe when the aggregate is mergeable."""
    name = "聚合结果正确 (mergeable aggregate)"
    value = trace.facts.get("aggregate_value")
    truth = trace.facts.get("aggregate_truth")
    if value is None or truth is None:
        return CheckResult(name, True, "本次运行没有聚合结果可检查。")
    if abs(value - truth) > 1e-6:
        return CheckResult(
            name, False,
            f"下推后的结果是 {value}，集中计算的结果是 {truth}。"
            "把局部平均值再平均一次，只有在每个分区行数相同时才凑巧正确。",
        )
    return CheckResult(name, True, f"结果 {value}，与集中计算一致。")


def value_conserved(trace: Trace) -> CheckResult:
    """Transfers move value; they never create or destroy it."""
    name = "总额守恒 (value conserved)"
    initial = trace.facts.get("initial_total")
    final = trace.facts.get("final_total")
    if initial is None or final is None:
        return CheckResult(name, True, "本次运行没有余额数据。")
    if initial != final:
        return CheckResult(
            name, False,
            f"初始总额 {initial}，结束时 {final}，差 {final - initial}。"
            "有事务只生效了一半——转账的两端没有被绑在一起。",
        )
    return CheckResult(name, True, f"总额保持在 {final}。")


def log_structure_sound(trace: Trace) -> CheckResult:
    """Structural invariant: the suffix starts just above the snapshot and is dense.

    A stale message must never be able to write inside a compacted prefix, so this
    is checked on every replica rather than trusted.
    """
    name = "日志结构自洽 (log structure sound)"
    final = trace.facts.get("final", {})
    for node, state in sorted(final.items()):
        base = state.get("base", 0)
        indexes = [entry["index"] for entry in state.get("log", [])]
        if any(index <= base for index in indexes):
            return CheckResult(
                name, False,
                f"{node} 的快照位置是 {base}，日志里却仍有索引 {min(indexes)} 的条目——"
                "有消息写进了已被压缩的前缀。",
            )
        if indexes and indexes != list(range(base + 1, base + 1 + len(indexes))):
            return CheckResult(
                name, False,
                f"{node} 的日志索引不连续：快照位置 {base}，条目索引 {indexes}。",
            )
    return CheckResult(name, True, f"{len(final)} 个节点的快照位置与日志后缀都连续自洽。")


CHECKERS = {
    "linearizable": linearizable,
    "log_structure_sound": log_structure_sound,
    "value_conserved": value_conserved,
    "aggregate_result_correct": aggregate_result_correct,
    "join_complete": join_complete,
    "broadcast_fits_memory": broadcast_fits_memory,
    "recovery_correct": recovery_correct,
    "durable_commits": durable_commits,
    "load_balance": load_balance,
    "stable_routing": stable_routing,
    "routed_correctly": routed_correctly,
    "atomic_commit": atomic_commit,
    "no_unilateral_resolution": no_unilateral_resolution,
    "single_leader_per_term": single_leader_per_term,
    "committed_prefix_stable": committed_prefix_stable,
    "acked_not_lost": acked_not_lost,
    "at_most_once": at_most_once,
    "replicas_agree": replicas_agree,
}


def run_checks(trace: Trace, names: tuple[str, ...]) -> list[CheckResult]:
    return [CHECKERS[name](trace) for name in names]
