"""第5讲：下推与两阶段聚合 — 越早减少数据，搬得越少；但不是所有聚合都能下推。

四档下推逐级减少跨网络的数据量。可合并的聚合（SUM/COUNT/MIN/MAX）适合分层执行；
把局部平均值再平均一次则会悄悄给出错误答案。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..kernel import Kernel
from ..nodes.query import (AVG_DECOMPOSED, AVG_NAIVE, FULL, NONE, PREDICATE,
                           PREDICATE_PROJECTION, SUM, make_rows, run_aggregation)
from ..params import SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ..trace import Trace

PARAMS = ParamSet((
    Param("pushdown", "下推程度 (pushdown)", "choice", NONE, choices=(
        (NONE, "不下推：全部行汇集到协调者"),
        (PREDICATE, "谓词下推：各节点先过滤"),
        (PREDICATE_PROJECTION, "谓词 + 投影下推"),
        (FULL, "再加局部聚合（两阶段）"),
    ), axis=True),
    Param("aggregate", "聚合函数 (aggregate)", "choice", SUM, choices=(
        (SUM, "SUM：可合并"),
        (AVG_DECOMPOSED, "AVG：拆成 SUM 与 COUNT 再合并"),
        (AVG_NAIVE, "AVG：局部求平均，再把平均值平均（错误）"),
    ), axis=True),
    Param("rows", "总行数 (rows)", "int", 600, minimum=100, maximum=4000),
    Param("workers", "执行节点数 (workers)", "int", 4, minimum=2, maximum=8),
    Param("bandwidth", "带宽 (bytes/tick)", "int", 4096, minimum=256, maximum=65536),
    SEED,
))

END = 900


def run(params: dict[str, Any], seed: int) -> RunResult:
    rows = make_rows(seed, params["rows"], 40, 0.3, "T")
    result = run_aggregation(params["pushdown"], params["aggregate"], rows,
                             params["workers"], params["bandwidth"], 1, 40)

    trace = Trace("pushdown-aggregation", seed, params, ["dataflow"], title="下推与两阶段聚合")
    kernel = Kernel(seed)
    trace.facts["plan"] = {"nodes": result.plan.nodes, "edges": result.plan.edges}
    trace.facts["aggregate_value"] = result.value
    trace.facts["aggregate_truth"] = result.truth
    for worker in range(params["workers"]):
        trace.add_node(f"w{worker}", kind="worker")

    trace.note(0,
               "查询是 “筛选出 gold 行，再对金额做聚合”。四档下推的差别只有一个："
               "在把数据搬到协调者之前，各节点先做了多少工作。",
               level="phase", title="设定：一个过滤 + 一个聚合")

    stages = ["扫描本地分区", "本地过滤与投影", "局部聚合", "汇总并计算最终结果"]
    for layer, label in enumerate(stages):
        kernel.schedule_at(160 * (layer + 1), lambda layer=layer, label=label:
                           trace.record(kernel.now, "flow.stage", layer=layer, label=label))

    def cost_note() -> None:
        trace.note(kernel.now,
                   f"这一档下推之后，跨网络的是 {result.network_rows} 行 / "
                   f"{result.network_bytes} 字节。把四档都跑一遍，"
                   "记下扫描行数、网络字节和协调者收到的中间行数——这三个数才是计划的真实代价。",
                   level="teach", title="搬了多少")

    kernel.schedule_at(560, cost_note)

    def verdict() -> None:
        if abs(result.value - result.truth) > 1e-6:
            trace.note(kernel.now,
                       f"下推后的答案是 {result.value}，集中计算是 {result.truth}。"
                       "局部平均值再平均，只有在每个分区行数完全相同时才凑巧正确。"
                       "可合并聚合才适合分层执行；AVG 要拆成 SUM 与 COUNT。",
                       level="violation", title="这个聚合不能这样下推")
        elif params["pushdown"] == FULL:
            trace.note(kernel.now,
                       f"结果 {result.value} 与集中计算一致，而跨网络只有 "
                       f"{result.network_rows} 条部分状态。"
                       "这就是可合并聚合的价值：中间结果被压在了本地。",
                       level="teach", title="可合并聚合的收益")
        else:
            trace.note(kernel.now,
                       f"结果 {result.value} 正确。继续往下推一档，看网络字节还能降多少。",
                       level="teach", title="还可以再往下推")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    checks = run_checks(trace, ("aggregate_result_correct",))
    metrics = {
        "下推程度 (pushdown)": params["pushdown"],
        "扫描行数 (scanned)": result.scanned,
        "跨网络行数 (rows over network)": result.network_rows,
        "跨网络字节 (bytes)": result.network_bytes,
        "协调者收到的中间行数": result.peak_intermediate,
        "结果 / 正确结果": f"{result.value} / {result.truth}",
        "估计耗时 (ticks)": result.elapsed,
    }
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="pushdown-aggregation",
    order=51,
    lecture="第5讲 分布式查询",
    title="下推与两阶段聚合",
    subtitle="越早减少数据，搬得越少",
    views=("dataflow",),
    params=PARAMS,
    run=run,
    question="把过滤、投影和聚合逐级下推，网络上少搬多少数据？哪一种聚合不能这样下推？",
    takeaway="谓词与投影下推减少搬运的行数与字节；局部聚合把中间结果压在本地，"
             "跨网络的只剩每个节点一条部分状态。前提是聚合可合并："
             "SUM/COUNT/MIN/MAX 天然可以，AVG 必须拆成 SUM 与 COUNT，"
             "中位数这类算子则需要更复杂的状态或近似算法。",
    prediction=Prediction(
        question="每个节点先算出本地平均值，协调者再把这些平均值平均一下。结果对吗？",
        options=("对，平均的平均还是平均", "只有各分区行数相同时才对",
                 "永远错", "取决于聚合下推的实现"),
        answer=1,
        why="平均值丢掉了行数这个权重。只有当每个分区参与计算的行数完全相同时，"
            "平均的平均才凑巧等于整体平均。正确做法是下推 SUM 与 COUNT，最后再相除。",
    ),
    invariants=("aggregate_result_correct",),
    faults=(),
    caveat="代价模型是教学化的（固定行宽、线性带宽），用于比较下推档位之间的量级。",
))
