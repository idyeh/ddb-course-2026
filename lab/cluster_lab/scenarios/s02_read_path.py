"""第2讲 / 第6讲：读路径 — “从 Leader 读”和“从副本读”都不是一致性等级。

同一段分区剧本，同一批读请求，只换读协议。区别不在于谁来应答，
而在于应答前它必须先证明什么。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..nodes.client import Client
from ..nodes.kv import put, read
from ..nodes.raft import READ_FOLLOWER, READ_INDEX, READ_LEADER_ONLY, READ_LEASE
from ..params import DELAY_MAX, DELAY_MIN, DROP, SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ._common import build, campaign, metrics_for

PARAMS = ParamSet((
    Param("read", "读协议 (read protocol)", "choice", READ_LEADER_ONLY, choices=(
        (READ_LEADER_ONLY, "直接从 Leader 读（不作证明）"),
        (READ_INDEX, "ReadIndex：先确认领导权"),
        (READ_LEASE, "租约读：依赖时钟假设"),
        (READ_FOLLOWER, "Follower Read：先追上提交位置"),
    ), help="四种读路径跑同一段剧本。", axis=True),
    Param("clock_skew", "旧 Leader 时钟倒退 (clock jump)", "choice", "off", choices=(
        ("off", "时钟正常"),
        ("on", "被隔离的旧 Leader 时钟倒退"),
    ), help="只影响租约读：租约的安全性建立在时钟假设上。", axis=True),
    Param("lease", "租约时长 (lease)", "int", 220, minimum=60, maximum=1000),
    Param("client_timeout", "客户端超时 (client timeout)", "int", 250, minimum=80, maximum=1200),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

NODES = ["A", "B", "C", "D", "E"]
END = 2600
SKEW = -1200


def run(params: dict[str, Any], seed: int) -> RunResult:
    policy = params["read"]
    kernel, trace, network, cluster = build(
        "read-path", "读路径", params, seed, NODES,
        views=("cluster", "history"), auto_election=False,
        read_policy=policy, lease=params["lease"],
    )
    client = Client("客户端", kernel, trace, network, timeout=params["client_timeout"])
    stale_side = "B" if policy == READ_FOLLOWER else "A"

    trace.note(0,
               "A 先成为 Leader 并写入 x=1。随后网络裂开，多数派选出新 Leader 并写入 x=2。"
               "关键时刻是分区之后：向落后一侧发出的读，会得到什么？",
               level="phase", title="设定：读在分区两侧各发一次")

    campaign(kernel, cluster, "A", 20)
    kernel.schedule_at(200, lambda: client.submit(put("op-1", "req-1", "x", 1), "A"))

    def cut() -> None:
        network.partition([["A", "B"], ["C", "D", "E"]])
        if params["clock_skew"] == "on":
            cluster.clock_offset["A"] = SKEW
            trace.note(kernel.now,
                       "同时让 A 的时钟大幅倒退。A 因此认为自己的租约还没到期——"
                       "租约读的安全性完全建立在这个时钟假设上。",
                       level="warn", title="时钟跳变")
        trace.note(kernel.now,
                   "分区成 {A B} 与 {C D E}。A 仍自认为是 Leader，它的数据停在 x=1。",
                   level="phase", title="第二幕：分区")

    kernel.schedule_at(620, cut)
    campaign(kernel, cluster, "C", 700)
    kernel.schedule_at(950, lambda: client.submit(put("op-2", "req-2", "x", 2), "C"))

    def stale_read() -> None:
        client.submit(read("op-3", "req-3", "x"), stale_side)
        trace.note(kernel.now,
                   f"x=2 已经写入成功并返回 ok。现在向落后一侧的 {stale_side} 发一次读——"
                   "它能不能证明自己有资格回答？",
                   level="phase", title="第三幕：向落后一侧读")

    kernel.schedule_at(1250, stale_read)
    kernel.schedule_at(1600, lambda: client.submit(read("op-4", "req-4", "x"), "E"))
    kernel.schedule_at(1620, lambda: trace.note(
        kernel.now,
        "同一时刻向多数派一侧的 Follower E 发一次读。位置相同都是“副本”，"
        "但读协议不同，承诺就不同。",
        level="teach", title="从副本读 ≠ 弱一致"))

    kernel.schedule_at(1950, lambda: (
        network.heal(),
        cluster.clock_offset.pop("A", None),
        trace.note(kernel.now, "网络恢复，A 退位为 Follower。", level="phase", title="第四幕：恢复"),
    ))
    kernel.schedule_at(2250, lambda: client.submit(read("op-5", "req-5", "x"), "A"))

    def verdict() -> None:
        outcomes = {item["opId"]: item for item in client.outcomes()}
        stale = outcomes.get("op-3", {})
        if stale.get("status") == "ok" and stale.get("result") == 1:
            trace.note(kernel.now,
                       "落后一侧回答了 x=1。可是 x=2 早已成功返回——"
                       "这个读把时间倒了回去，历史不再线性一致。",
                       level="violation", title="陈旧读")
        elif stale.get("status") == "fail":
            trace.note(kernel.now,
                       "落后一侧明确拒绝了这次读。拒绝是一个诚实的答案："
                       "它比返回一个无法证明的值要好。",
                       level="teach", title="拒绝服务，而不是猜")
        else:
            trace.note(kernel.now,
                       "落后一侧没能证明自己的资格，读请求超时变成 unknown。"
                       "代价是可用性，换来的是不返回陈旧值。",
                       level="teach", title="宁可等待，不返回旧值")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    trace.facts["final"] = cluster.snapshot()
    trace.facts["client"] = client.outcomes()
    checks = run_checks(trace, ("linearizable", "acked_not_lost", "single_leader_per_term"))
    metrics = metrics_for(cluster, network, [client])
    served = [f"{item['opId']}→{item.get('served_by') or item.get('status')}"
              for item in client.outcomes() if item.get("kind") == "read"]
    metrics["读取应答方"] = "，".join(served)
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="read-path",
    order=22,
    lecture="第2讲 复制与共识",
    title="读路径",
    subtitle="ReadIndex、租约与 Follower Read",
    views=("cluster", "history"),
    params=PARAMS,
    run=run,
    question="“从 Leader 读”能保证读到最新已提交结果吗？“从副本读”一定是弱一致吗？",
    takeaway="位置不决定一致性，证明才决定。直接从 Leader 读什么都没证明；"
             "ReadIndex 先确认领导权再等待应用；租约把安全性押在时钟假设上；"
             "Follower Read 先追上提交位置，因此“读副本”同样可以是强一致。",
    prediction=Prediction(
        question="被隔离的旧 Leader 收到一次读请求，哪种读协议会返回陈旧值？",
        options=("ReadIndex", "直接从 Leader 读", "Follower Read", "四种都会"),
        answer=1,
        why="直接从 Leader 读不做任何领导权证明，旧 Leader 会用自己的本地状态作答。"
            "租约读在时钟假设成立时是安全的——把时钟跳变打开，它也会返回陈旧值。",
    ),
    invariants=("linearizable", "acked_not_lost", "single_leader_per_term"),
    faults=("partition", "clock_jump", "drop"),
))
