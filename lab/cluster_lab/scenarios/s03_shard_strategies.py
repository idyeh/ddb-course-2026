"""第3讲：分片策略对比 — 均衡与局部性是一对互相拉扯的目标。

同一批倾斜请求，四种分片键设计。看两个数：最热分片的负载比，
以及一次单租户扫描要触及多少个分片。没有一种设计能同时把两个数都压到最低。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..invariants import run_checks
from ..kernel import Kernel
from ..nodes.routing import (COMPOSITE, HASH, RANGE, SALTED, STRATEGY_LABELS,
                             scan_shards, shard_for, tenant_of, workload)
from ..params import SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ..trace import Trace

HOT = "tenant-1"
THRESHOLD = 2.5

PARAMS = ParamSet((
    Param("strategy", "分片策略 (strategy)", "choice", RANGE, choices=(
        (RANGE, "Range：按键顺序切分"),
        (HASH, "Hash：整键稳定散列"),
        (COMPOSITE, "复合键：按租户共置"),
        (SALTED, "加盐：只打散热点租户"),
    ), axis=True),
    Param("shards", "分片数 (shards)", "int", 6, minimum=2, maximum=12),
    Param("requests", "请求数 (requests)", "int", 180, minimum=30, maximum=600),
    Param("hot_share", "热点租户占比 (hot share)", "float", 0.6, minimum=0.0, maximum=0.95,
          step=0.05, help="课件里的倾斜负载：一个大租户占据大部分流量。"),
    Param("tenants", "租户数 (tenants)", "int", 100, minimum=5, maximum=500),
    Param("salt", "盐桶数 (salt buckets)", "int", 12, minimum=2, maximum=32,
          help="仅在加盐策略下生效。"),
    SEED,
))


def run(params: dict[str, Any], seed: int) -> RunResult:
    strategy = params["strategy"]
    shards = params["shards"]
    trace = Trace("shard-strategies", seed, params, ["shards"], title="分片策略对比")
    kernel = Kernel(seed)
    shard_ids = [f"S{index}" for index in range(shards)]
    for shard_id in shard_ids:
        trace.add_node(shard_id, kind="shard")
    trace.add_client("路由器")

    trace.note(0,
               f"{STRATEGY_LABELS[strategy]}。请求流是倾斜的：约 "
               f"{int(params['hot_share'] * 100)}% 落在 {HOT} 上。"
               "下方柱状图是各分片累计承担的请求数，虚线是均衡阈值。",
               level="phase", title="设定：倾斜的请求流")

    keys = workload(seed, params["requests"], params["tenants"], params["hot_share"], HOT)
    counts: Counter[str] = Counter()
    gap = max(4, 2000 // max(1, len(keys)))
    for number, key in enumerate(keys):
        at = 60 + number * gap
        shard_id = shard_ids[shard_for(key, strategy, shards, HOT, params["salt"])]
        counts[shard_id] += 1
        trace.record(at, "shard.route", key=key, tenant=tenant_of(key), shard=shard_id,
                     hot=tenant_of(key) == HOT, arriveAt=at + 26)

    end = 60 + len(keys) * gap + 120
    load = {shard_id: counts.get(shard_id, 0) for shard_id in shard_ids}
    trace.facts["shard_load"] = load
    trace.facts["balance_threshold"] = THRESHOLD

    average = sum(load.values()) / len(load)
    hottest = max(load, key=lambda shard: load[shard])
    ratio = load[hottest] / average if average else 0
    hot_scan = scan_shards(strategy, shards, params["salt"], hot=True)
    cold_scan = scan_shards(strategy, shards, params["salt"], hot=False)

    verdicts = {
        RANGE: "相邻键落在相邻范围，扫描和前缀查询很高效；代价是热点租户全部压在一个分片上。",
        HASH: "整键散列把负载摊平了，但也把同一租户的数据打散："
              "任何单租户扫描都要touch所有分片。",
        COMPOSITE: "复合键让同租户共置，事务和扫描都便宜；"
                   "但大租户会独占一个分片，均衡问题原封不动。",
        SALTED: "只把热点租户散到多个桶：普通租户保留共置，热点租户换来均衡。"
                "代价是热点租户的扫描要在读侧合并多个分片。",
    }
    trace.note(end - 80,
               verdicts[strategy].replace("touch", "触及"),
               level="teach", title=f"{strategy} 的取舍")
    trace.note(end - 40,
               f"最热分片比值 {ratio:.2f}；热点租户扫描触及 {hot_scan} 个分片，"
               f"普通租户扫描触及 {cold_scan} 个。"
               "把四种策略都跑一遍，把这两个数抄进表格——它们从来不会同时最优。",
               level="phase", title="记下这两个数")
    kernel.run(end)

    checks = run_checks(trace, ("load_balance", "stable_routing"))
    metrics = {
        "策略 (strategy)": strategy,
        "最热分片": f"{hottest}（{load[hottest]} 个请求）",
        "最热/平均": f"{ratio:.2f}",
        "热点租户扫描触及分片": hot_scan,
        "普通租户扫描触及分片": cold_scan,
        "各分片负载": "，".join(f"{shard}:{count}" for shard, count in sorted(load.items())),
    }
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="shard-strategies",
    order=30,
    lecture="第3讲 分区与分片",
    title="分片策略对比",
    subtitle="Range / Hash / 复合键 / 加盐",
    views=("shards",),
    params=PARAMS,
    run=run,
    question="同一批倾斜请求，四种分片键设计各自把什么优化掉了，又牺牲了什么？",
    takeaway="分片设计是在选择局部性与冲突域，不是在找“最均匀的那个”。"
             "Range 与复合键保留局部性、留下热点；整键 Hash 摊平负载、丢掉全部局部性；"
             "加盐只打散热点键，用热点租户的扫描成本换整体均衡。"
             "评审分片键时，请同时报出最热分片比值和扫描触及的分片数。",
    prediction=Prediction(
        question="把整个键做散列，负载确实摊平了。这样做付出了什么代价？",
        options=("没有代价，散列总是更好", "同一租户的数据被打散，单租户扫描要触及所有分片",
                 "写入会变慢", "分片数不能再改变"),
        answer=1,
        why="散列破坏了键的顺序与前缀局部性。均衡与局部性是一对互相拉扯的目标，"
            "分片键决定了未来查询计划的空间。",
    ),
    invariants=("load_balance", "stable_routing"),
    faults=(),
    caveat=f"均衡检查使用明确阈值：最热分片不超过平均值的 {THRESHOLD} 倍。阈值是声明出来的，不是客观标准。",
))
