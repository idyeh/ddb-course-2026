"""第2讲：Raft 选举 — 随机超时如何打破对称，任期如何保护安全性。"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..params import (DELAY_MAX, DELAY_MIN, DROP, HEARTBEAT, SEED, TIMEOUT_MAX,
                      TIMEOUT_MIN, Param, ParamSet, nodes_param)
from ..registry import Prediction, RunResult, Scenario, register
from ._common import build, metrics_for

NODE_NAMES = ["A", "B", "C", "D", "E", "F", "G"]

PARAMS = ParamSet((
    nodes_param(5),
    Param("partition", "网络分区 (partition)", "choice", "leader_alone", choices=(
        ("none", "不分区"),
        ("leader_alone", "隔离当前 Leader"),
        ("minority", "隔离少数派"),
    ), help="分区在演示中段发生，末段恢复。", axis=True),
    DROP, TIMEOUT_MIN, TIMEOUT_MAX, HEARTBEAT, DELAY_MIN, DELAY_MAX,
    Param("duration", "演示时长 (ticks)", "int", 1600, minimum=400, maximum=6000),
    SEED,
))


def run(params: dict[str, Any], seed: int) -> RunResult:
    node_ids = NODE_NAMES[: params["nodes"]]
    kernel, trace, network, cluster = build(
        "raft-election", "Raft 选举", params, seed, node_ids, auto_election=True,
    )
    duration = params["duration"]

    trace.note(0, "每个 Follower 在随机超时后参选。固定超时会让多个节点反复同时参选，"
                  "随机化把碰撞概率压下去。观察小球：RequestVote 出去，投票回来。",
               level="phase", title="第一幕：选举超时打破对称")

    if params["partition"] != "none":
        cut = int(duration * 0.35)
        heal = int(duration * 0.72)

        def make_partition() -> None:
            leader = cluster.leader()
            if params["partition"] == "leader_alone" and leader is not None:
                isolated = [leader.id]
                rest = [node for node in node_ids if node != leader.id]
                trace.note(kernel.now,
                           f"把 Leader {leader.id} 单独隔离。它仍然自认为是任期 "
                           f"{leader.current_term} 的 Leader，却再也凑不齐多数派："
                           "既不能提交新条目，也不知道外面已经换届。",
                           level="phase", title="第二幕：隔离 Leader")
            else:
                half = len(node_ids) // 2
                isolated = node_ids[:half]
                rest = node_ids[half:]
                trace.note(kernel.now,
                           f"隔离少数派 {'、'.join(isolated)}。少数派会不断超时、递增任期、"
                           "反复参选，却永远拿不到多数票。",
                           level="phase", title="第二幕：隔离少数派")
            network.partition([isolated, rest])

        kernel.schedule_at(cut, make_partition)
        kernel.schedule_at(heal, lambda: (
            network.heal(),
            trace.note(kernel.now,
                       "网络恢复。回到集群的旧 Leader 一看到更高任期就退位成 Follower——"
                       "任期是单调递增的凭证，不需要谁来仲裁。",
                       level="phase", title="第三幕：恢复与退位"),
        ))

    kernel.run(duration)
    trace.facts["final"] = cluster.snapshot()

    checks = run_checks(trace, ("single_leader_per_term", "committed_prefix_stable"))
    metrics = metrics_for(cluster, network, [])
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="raft-election",
    order=20,
    lecture="第2讲 复制与共识",
    title="Raft 选举",
    subtitle="随机超时、任期与投票限制",
    views=("cluster",),
    params=PARAMS,
    run=run,
    question="五个节点同时启动，谁来当 Leader？把当前 Leader 网络隔离之后，集群里会不会同时存在两个 Leader？",
    takeaway="同一时刻确实可能有两个节点自称 Leader，但它们的任期 (term) 不同。"
             "安全性要求的是“每个任期至多一个 Leader”，而不是“任何时刻至多一个 Leader”。",
    prediction=Prediction(
        question="隔离当前 Leader 之后，集群里最多会有几个自称 Leader 的节点？",
        options=("0 个，写入完全停止", "1 个，旧 Leader 立刻发现自己被隔离", "2 个，任期不同", "2 个，任期相同"),
        answer=2,
        why="旧 Leader 收不到任何消息，无法得知外界已经换届，会一直自认为是 Leader；"
            "多数派那边则选出了更高任期的新 Leader。两者任期不同，安全性不被破坏。",
    ),
    invariants=("single_leader_per_term", "committed_prefix_stable"),
    faults=("partition", "drop", "delay"),
))
