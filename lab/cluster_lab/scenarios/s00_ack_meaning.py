"""绪论：确认是何意味 — 客户端收到 ok 的那一刻，系统到底承诺了什么。

这是绪论“第一个实验”那一页的可执行版本。两个开关各自暴露一类问题：
确认点决定已确认写会不会丢，请求编号决定重试会不会重复生效。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..nodes.client import Client
from ..nodes.kv import incr
from ..nodes.raft import ACK_LOCAL, ACK_MAJORITY
from ..params import DELAY_MAX, DELAY_MIN, DROP, Param, ParamSet, SEED
from ..registry import Prediction, RunResult, Scenario, register
from ._common import build, campaign, metrics_for

PARAMS = ParamSet((
    Param("ack", "确认点 (acknowledgement point)", "choice", ACK_MAJORITY, choices=(
        (ACK_MAJORITY, "多数派确认后回复（安全）"),
        (ACK_LOCAL, "Leader 本地落盘就回复（低延迟）"),
    ), help="决定客户端听到 ok 时，这条写入究竟到了哪里。", axis=True),
    Param("retry", "重试策略 (retry)", "choice", "same_id", choices=(
        ("same_id", "复用同一个 request_id"),
        ("new_id", "每次重试生成新的 request_id"),
    ), help="超时后客户端并不知道结果，只能重试。", axis=True),
    Param("client_timeout", "客户端超时 (client timeout)", "int", 250, minimum=60, maximum=1200),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

DURATION = 2100


def run(params: dict[str, Any], seed: int) -> RunResult:
    kernel, trace, network, cluster = build(
        "ack-meaning", "确认是何意味", params, seed, ["A", "B", "C"],
        views=("cluster", "history"), auto_election=False, ack=params["ack"],
    )
    client = Client("客户端", kernel, trace, network, timeout=params["client_timeout"])

    trace.note(0,
               "三个副本维护同一个计数器。全程只有一个客户端，它会重试，"
               "但每次重试都可能撞上不同的故障。请盯住三件事：谁回复了 ok、"
               "这条记录到了几个副本、最终计数器是多少。",
               level="phase", title="设定：三副本计数器")
    if params["ack"] == ACK_LOCAL:
        trace.note(0,
                   "本次确认点设为 local：Leader 只要把条目写进自己的日志就回复 ok，"
                   "不等待复制。正常路径确实更快。",
                   level="warn", title="确认点 = local")

    campaign(kernel, cluster, "A", 20)
    kernel.schedule_at(200, lambda: client.submit(incr("op-1", "req-1"), "A"))

    def cut() -> None:
        network.partition([["A"], ["B", "C"]])
        trace.note(kernel.now,
                   "把 A 单独隔离。A 仍自认为是 Leader，可以继续接收请求，"
                   "但它的 AppendEntries 到不了任何人。",
                   level="phase", title="第二幕：Leader 被隔离")

    kernel.schedule_at(400, cut)
    kernel.schedule_at(430, lambda: client.submit(incr("op-2", "req-2"), "A"))

    def after_op2() -> None:
        if params["ack"] == ACK_LOCAL:
            trace.note(kernel.now,
                       "op-2 已经拿到 ok，可它此刻只存在于一个被隔离的副本上。"
                       "客户端得到的承诺，和系统能守住的承诺，已经不是同一件事。",
                       level="warn", title="一个危险的 ok")
        else:
            trace.note(kernel.now,
                       "op-2 超时返回 unknown。注意：unknown 不是 fail。"
                       "这条请求可能没到达、可能正在执行、也可能已经执行但回复丢了。",
                       level="teach", title="超时 = 未知")

    kernel.schedule_at(700, after_op2)
    campaign(kernel, cluster, "B", 800)
    kernel.schedule_at(1000, lambda: client.submit(incr("op-3", "req-3"), "B"))

    def heal() -> None:
        network.heal()
        trace.note(kernel.now,
                   "网络恢复。B 的 AppendEntries 带着更高任期到达 A，A 退位；"
                   "前缀检查发现冲突，A 截断自己那条未提交的日志。",
                   level="phase", title="第三幕：恢复与截断")

    kernel.schedule_at(1200, heal)

    def lose_reply() -> None:
        network.censor_once(
            "reply_lost",
            lambda source, target, message: (
                message.get("type") == "ClientReply" and message.get("reqId") == "req-4"
            ),
        )
        trace.note(kernel.now,
                   "接下来 op-4 会被正常提交，但它的回复会在网络上丢失。"
                   "客户端只知道自己超时了，必须重试。",
                   level="phase", title="第四幕：回复丢失后的重试")

    kernel.schedule_at(1400, lose_reply)
    kernel.schedule_at(1420, lambda: client.submit(incr("op-4", "req-4"), "B"))

    def retry() -> None:
        request_id = "req-4" if params["retry"] == "same_id" else "req-4-retry"
        client.submit(incr("op-4", request_id), "B")
        if params["retry"] == "same_id":
            trace.note(kernel.now,
                       "重试复用 req-4。Leader 在去重表里查到它，直接返回旧结果，"
                       "计数器不会被加第二次。",
                       level="teach", title="复用 request_id")
        else:
            trace.note(kernel.now,
                       "重试换了一个新的 request_id。对状态机来说这是一条全新命令，"
                       "去重表无从识别——同一个业务操作即将生效两次。",
                       level="warn", title="新 request_id 绕过去重")

    kernel.schedule_at(1750, retry)
    kernel.run(DURATION)

    trace.facts["final"] = cluster.snapshot()
    trace.facts["client"] = client.outcomes()
    logical_ops = {outcome["opId"] for outcome in client.outcomes()}
    leader = cluster.leader()
    counter = leader.machine.counter if leader else 0
    trace.note(DURATION,
               f"运行结束：客户端一共发出 {len(logical_ops)} 个业务操作，"
               f"最终计数器为 {counter}。两者对不上时，问题一定出在确认点或请求编号。",
               level="phase", title="对账")

    checks = run_checks(trace, ("acked_not_lost", "at_most_once", "committed_prefix_stable"))
    metrics = metrics_for(cluster, network, [client])
    metrics["业务操作数 (logical ops)"] = len(logical_ops)
    metrics["最终计数器 (counter)"] = counter
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="ack-meaning",
    order=0,
    lecture="绪论",
    title="确认是何意味",
    subtitle="确认点、超时与请求编号",
    views=("cluster", "history"),
    params=PARAMS,
    run=run,
    question="客户端收到 ok，这条写入到底存在于几个副本上？收到超时，又能推出什么？",
    takeaway="ok 的含义由确认点定义，不是由“看起来成功了”定义；"
             "超时只能推出 unknown；而重试必须复用同一个 request_id，"
             "去重表还必须是复制状态的一部分。",
    prediction=Prediction(
        question="确认点设为 local（Leader 本地落盘即回复）时，下面哪一项会被打破？",
        options=("同一操作至多生效一次", "已确认写不丢失", "每个任期至多一个 Leader", "什么都不会被打破"),
        answer=1,
        why="被隔离的 Leader 在复制之前就回复了 ok。新 Leader 选出后，"
            "那条日志因为前缀冲突被截断，客户端手里的 ok 变成了一句空话。",
    ),
    invariants=("acked_not_lost", "at_most_once", "committed_prefix_stable"),
    faults=("partition", "drop", "reply_lost"),
))
