"""第1讲：日志相同，状态不同 — 状态机命令必须可确定性重放。

三个副本收到**逐字节相同**的日志。如果 apply() 里调用了随机数或读了本地时钟，
相同的输入就会产生不同的状态；把决定提前到排序之前、写进命令里，才谈得上重放。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..nodes.client import Client
from ..nodes.kv import token
from ..params import DELAY_MAX, DELAY_MIN, DROP, SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ._common import build, campaign, metrics_for

AT_APPLY = "at_apply"
PREDECIDED = "predecided"

PARAMS = ParamSet((
    Param("mode", "决定在哪里做出 (where decided)", "choice", AT_APPLY, choices=(
        (AT_APPLY, "在 apply() 里现场生成（错误）"),
        (PREDECIDED, "排序前决定，写进命令（正确）"),
    ), help="两种模式复制的日志完全相同。", axis=True),
    Param("writes", "写入次数 (writes)", "int", 3, minimum=1, maximum=8),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

NODES = ["A", "B", "C"]
END = 1800


def run(params: dict[str, Any], seed: int) -> RunResult:
    at_apply = params["mode"] == AT_APPLY
    kernel, trace, network, cluster = build(
        "nondeterministic-apply", "相同日志，不同状态", params, seed, NODES,
        auto_election=False,
    )
    client = Client("客户端", kernel, trace, network, timeout=300)

    trace.note(0,
               "三个副本将收到完全相同的一串命令。请特别注意日志矩阵："
               "它在两种模式下一模一样。不同的只是每个副本应用之后的状态。",
               level="phase", title="设定：相同的输入")
    if at_apply:
        trace.note(0,
                   "本次 apply() 会在执行时现场生成 token（相当于调用 random() 或读本地时钟）。"
                   "每个副本应用的时刻不同，身份也不同。",
                   level="warn", title="决定被推迟到了 apply()")
    else:
        trace.note(0,
                   "本次 token 的值在进入日志之前就已确定，并作为命令数据被复制。"
                   "谁在什么时候重放它，结果都一样。",
                   level="teach", title="决定被提前到排序之前")

    campaign(kernel, cluster, "A", 20)
    for number in range(params["writes"]):
        name = f"t{number + 1}"
        value = None if at_apply else f"预定值-{number + 1}"
        command = token(f"op-{number + 1}", f"req-{number + 1}", name, value)
        kernel.schedule_at(250 + number * 260,
                           lambda command=command: client.submit(command, "A"))

    def verdict() -> None:
        states = {node_id: node.machine.summary()
                  for node_id, node in cluster.nodes.items() if node.alive}
        distinct = set(states.values())
        listing = "；".join(f"{node}: {state}" for node, state in sorted(states.items()))
        if len(distinct) > 1:
            trace.note(kernel.now,
                       f"三个副本的日志相同，状态却分成了 {len(distinct)} 种：{listing}。"
                       "状态机复制的公式是“相同初态 + 相同命令顺序 ⇒ 相同状态”，"
                       "前提是命令可确定性应用——这里的前提不成立。",
                       level="violation", title="副本发散")
        else:
            trace.note(kernel.now,
                       f"三个副本状态一致：{listing}。"
                       "随机值、时间戳、外部读取结果都应在排序前确定，并作为命令数据复制。",
                       level="teach", title="可以重放的日志")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    trace.facts["final"] = cluster.snapshot()
    trace.facts["client"] = client.outcomes()
    checks = run_checks(trace, ("replicas_agree", "committed_prefix_stable", "at_most_once"))
    metrics = metrics_for(cluster, network, [client])
    states = {node.machine.summary() for node in cluster.nodes.values() if node.alive}
    metrics["不同状态数 (distinct states)"] = len(states)
    for node_id, node in sorted(cluster.nodes.items()):
        metrics[f"{node_id} 的状态"] = node.machine.summary() or "空"
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="nondeterministic-apply",
    order=11,
    lecture="第1讲 时间与日志",
    title="相同日志，不同状态",
    subtitle="状态机命令必须可确定性重放",
    views=("cluster",),
    params=PARAMS,
    run=run,
    question="三个副本收到逐字节相同的日志，它们的状态一定相同吗？",
    takeaway="“相同初态 + 相同命令顺序 ⇒ 相同状态”是一个带前提的公式：命令必须可确定性应用。"
             "随机结果、时间戳和外部读取都要在排序之前确定，并作为命令数据一起复制；"
             "否则日志一致，状态照样发散，而且没有任何协议层的检查会发现它。",
    prediction=Prediction(
        question="apply() 里读了本地时钟，三个副本的日志仍然完全相同。它们的状态会怎样？",
        options=("一定相同，日志相同就够了", "发散成不同状态", "Raft 会检测到并报错", "只有 Leader 是对的"),
        answer=1,
        why="Raft 保证的是日志一致，不是应用结果一致。"
            "副本在不同时刻、以不同身份执行同一条命令，读到的本地时钟自然不同，"
            "而共识层对此一无所知。",
    ),
    invariants=("replicas_agree", "committed_prefix_stable", "at_most_once"),
    faults=("drop", "delay"),
))
