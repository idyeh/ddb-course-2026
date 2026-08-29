"""Invariant checkers. Every one of them reads the trace, not the simulator state.

That is the discipline the course asks for: a conclusion has to be derivable from
the recorded history, so the same evidence supports the animation, the test and
the report.
"""

from __future__ import annotations

from typing import Any

from examples.raft_rules import majority

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
    logs: dict[str, dict[int, tuple[int, str]]] = {}
    for event in trace.events:
        kind = event["type"]
        if kind == "log.commit":
            committed.setdefault(event["index"], event)
        elif kind == "log.append":
            node = event["node"]
            logs.setdefault(node, {})[event["index"]] = (event["term"], event["cmd"])
            known = committed.get(event["index"])
            if known and (known["term"], known["cmd"]) != (event["term"], event["cmd"]):
                return CheckResult(
                    "已提交前缀不被推翻 (committed prefix stable)", False,
                    f"索引 {event['index']} 已提交 term {known['term']} / {known['cmd']}，"
                    f"却在 t={event['t']} 被 {node} 换成 term {event['term']} / {event['cmd']}",
                    [known, event],
                )
        elif kind == "log.truncate":
            node_log = logs.setdefault(event["node"], {})
            for index in [index for index in node_log if index >= event["from"]]:
                del node_log[index]

    final = trace.facts.get("final", {})
    if final and committed:
        size = len(final)
        for index, known in sorted(committed.items()):
            survivors = sum(
                1 for state in final.values()
                for entry in state["log"]
                if entry["index"] == index and entry["term"] == known["term"]
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
    acked = [event for event in _events(trace, "client.return") if event["status"] == "ok"]
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


CHECKERS = {
    "single_leader_per_term": single_leader_per_term,
    "committed_prefix_stable": committed_prefix_stable,
    "acked_not_lost": acked_not_lost,
    "at_most_once": at_most_once,
    "replicas_agree": replicas_agree,
}


def run_checks(trace: Trace, names: tuple[str, ...]) -> list[CheckResult]:
    return [CHECKERS[name](trace) for name in names]
