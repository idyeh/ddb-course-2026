"""第5讲：分布式 Join 策略 — 共置、广播与重分区。

三种策略跑同一份数据。前两个数字是成本（网络字节、最慢节点），
第三个数字是正确性：在没有共置的数据上选择本地 Join，
返回的行数会**悄悄地少**，而计划名称看不出任何异常。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..kernel import Kernel
from ..nodes.query import BROADCAST, LOCAL, ROW_BYTES, SHUFFLE, make_rows, run_join
from ..params import SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ..trace import Trace

PARAMS = ParamSet((
    Param("strategy", "Join 策略 (join strategy)", "choice", LOCAL, choices=(
        (LOCAL, "本地 Join：只处理本节点分区"),
        (BROADCAST, "Broadcast：复制小表到每个节点"),
        (SHUFFLE, "Repartition：两边都按连接键重分布"),
    ), axis=True),
    Param("colocated", "两表是否按连接键共置 (colocated)", "choice", "no", choices=(
        ("yes", "已共置：同一个键在同一节点"),
        ("no", "未共置：按行分布"),
    ), axis=True),
    Param("workers", "执行节点数 (workers)", "int", 4, minimum=2, maximum=8),
    Param("left_rows", "左表行数 (left rows)", "int", 400, minimum=50, maximum=2000),
    Param("right_rows", "右表行数 (right rows)", "int", 120, minimum=10, maximum=2000,
          help="调大它，Broadcast 与 Repartition 的成本会交叉。", axis=True),
    Param("hot_share", "热键占比 (hot key share)", "float", 0.35, minimum=0.0, maximum=0.9,
          step=0.05),
    Param("memory_mb", "每节点内存上限 (memory)", "float", 0.2, minimum=0.02, maximum=8.0,
          step=0.02, help="Broadcast 要求每个节点放得下整张小表。"),
    Param("bandwidth", "带宽 (bytes/tick)", "int", 4096, minimum=256, maximum=65536),
    SEED,
))

END = 900


def run(params: dict[str, Any], seed: int) -> RunResult:
    strategy = params["strategy"]
    colocated = params["colocated"] == "yes"
    workers = params["workers"]

    left = make_rows(seed, params["left_rows"], 40, params["hot_share"], "L")
    right = make_rows(seed + 1, params["right_rows"], 40, params["hot_share"], "R")
    result = run_join(strategy, left, right, workers, colocated,
                      params["bandwidth"], 1, 40)

    trace = Trace("join-strategies", seed, params, ["dataflow"], title="分布式 Join 策略")
    kernel = Kernel(seed)
    trace.facts["plan"] = {"nodes": result.plan.nodes, "edges": result.plan.edges}
    for worker in range(workers):
        trace.add_node(f"w{worker}", kind="worker")

    trace.note(0,
               "先看数据放在哪里，再看计划要把多少字节搬到哪里去。"
               "下方的算子图里，交换 (exchange) 那一层就是跨节点搬运发生的地方。",
               level="phase", title="设定：两张表，四个执行节点")

    stage = 150
    for layer, label in enumerate(["扫描本地分区", "交换数据", "各节点 Join", "汇总结果"]):
        kernel.schedule_at(stage * (layer + 1), lambda layer=layer, label=label:
                           trace.record(kernel.now, "flow.stage", layer=layer, label=label))

    megabytes = result.small_side_bytes / 1_000_000
    trace.facts["broadcast_bytes"] = result.small_side_bytes * (workers - 1)
    trace.facts["memory_limit_bytes"] = int(params["memory_mb"] * 1_000_000)
    trace.facts["needs_broadcast_memory"] = result.small_side_bytes if strategy == BROADCAST else 0
    trace.facts["join_rows"] = result.rows_out
    trace.facts["join_truth"] = result.truth

    broadcast_cost = result.small_side_bytes * (workers - 1)
    shuffle_cost = (len(left) + len(right)) * ROW_BYTES

    def strategy_note() -> None:
        if strategy == LOCAL and not colocated:
            trace.note(kernel.now,
                       "本地 Join 只把每个节点自己的两份分区连起来。"
                       f"连接键相同但落在不同节点的行永远碰不到面："
                       f"结果 {result.rows_out} 行，正确答案是 {result.truth} 行。"
                       "没有报错，没有告警——只是少了行。",
                       level="violation", title="共置不成立时的本地 Join")
        elif strategy == LOCAL:
            trace.note(kernel.now,
                       "两表按连接键共置，本地 Join 不需要为连接搬运任何数据。"
                       "这也是分片键会影响未来查询计划空间的原因。",
                       level="teach", title="最便宜的那一种")
        elif strategy == BROADCAST:
            trace.note(kernel.now,
                       f"小表被复制到每个节点：{megabytes:.2f} MB × {workers - 1} = "
                       f"{broadcast_cost / 1_000_000:.2f} MB。"
                       "成本随节点数增长，而且估错“小表”会把内存和网络一起放大。",
                       level="teach", title="Broadcast 的成本边界")
        else:
            trace.note(kernel.now,
                       f"两边都按连接键重新分布，网络成本约等于两表之和 "
                       f"（{shuffle_cost / 1_000_000:.2f} MB）。"
                       "这是可扩展的通用方案，但要付序列化与倾斜的代价。",
                       level="teach", title="Repartition 的成本边界")

    kernel.schedule_at(stage * 2 + 20, strategy_note)

    skew = (max(result.worker_rows) / (sum(result.worker_rows) / workers)
            if sum(result.worker_rows) else 1)
    kernel.schedule_at(END - 60, lambda: trace.note(
        kernel.now,
        f"最慢节点输入 {max(result.worker_rows)} 行，平均 "
        f"{sum(result.worker_rows) / workers:.0f} 行，比值 {skew:.2f}。"
        "总字节相同的两个计划，尾延迟可以差很远——查询被最慢的那个分区拖住。",
        level="phase", title="别只看总字节"))
    kernel.run(END)

    checks = run_checks(trace, ("join_complete", "broadcast_fits_memory"))
    metrics = {
        "策略 (strategy)": strategy,
        "结果行数 / 正确行数": f"{result.rows_out} / {result.truth}",
        "网络字节 (network)": f"{result.network_bytes / 1_000_000:.2f} MB",
        "Broadcast 估计成本": f"{broadcast_cost / 1_000_000:.2f} MB",
        "Repartition 估计成本": f"{shuffle_cost / 1_000_000:.2f} MB",
        "更便宜的一种": "Broadcast" if broadcast_cost < shuffle_cost else "Repartition",
        "最慢节点输入行数": max(result.worker_rows),
        "最慢/平均": f"{skew:.2f}",
        "估计耗时 (ticks)": result.elapsed,
    }
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="join-strategies",
    order=50,
    lecture="第5讲 分布式查询",
    title="分布式 Join 策略",
    subtitle="共置、Broadcast 与 Repartition",
    views=("dataflow",),
    params=PARAMS,
    run=run,
    question="两张大表都很大但已按连接键共置，该选哪种 Join？如果它们其实没有共置呢？",
    takeaway="分布式 Join 首先是在管理数据移动：共置让本地 Join 成为可能，"
             "Broadcast 的成本随节点数增长，Repartition 的成本约等于两表之和。"
             "但真正危险的不是选贵了，而是**在没有共置的数据上选择本地 Join**——"
             "它不会报错，只会少返回行。",
    prediction=Prediction(
        question="两表没有按连接键共置，却选了本地 Join，会发生什么？",
        options=("查询报错", "结果正确，只是更慢", "结果悄悄少了行", "系统自动改成 Repartition"),
        answer=2,
        why="本地 Join 只连接每个节点自己的分区。连接键相同但分布在不同节点的行永远不会相遇，"
            "查询照常成功返回，只是结果不完整。",
    ),
    invariants=("join_complete", "broadcast_fits_memory"),
    faults=("skew",),
    caveat="代价模型是教学化的（固定行宽、线性带宽与 CPU），用于比较策略之间的量级，不是性能基准。",
))
