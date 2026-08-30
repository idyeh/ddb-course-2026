"""绪论：分区期间，协议必须选择如何失败。

同一段剧本，同一批请求，只换一个开关：写入是否需要多数派。
CP 一侧把无法证明安全的请求变成 unknown；AP 一侧两边都继续服务，
代价是历史不再线性一致，而且少数派那边已确认的写最终消失。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..nodes.client import Client
from ..nodes.kv import put, read
from ..nodes.raft import (READ_ANY_NODE, READ_INDEX, WRITE_ANY_NODE, WRITE_CONSENSUS)
from ..params import DELAY_MAX, DELAY_MIN, DROP, SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ._common import build, campaign, metrics_for

CP = "cp_majority"
AP = "ap_any_node"

PARAMS = ParamSet((
    Param("policy", "分区期间的策略 (policy)", "choice", CP, choices=(
        (CP, "CP：只服务能证明安全的请求"),
        (AP, "AP：任何副本都接受读写"),
    ), help="两种策略跑的是同一段剧本、同一批请求。", axis=True),
    Param("client_timeout", "客户端超时 (client timeout)", "int", 250, minimum=80, maximum=1200),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

NODES = ["A", "B", "C", "D", "E"]
END = 2800


def run(params: dict[str, Any], seed: int) -> RunResult:
    ap = params["policy"] == AP
    kernel, trace, network, cluster = build(
        "cap-partition", "分区期间必须选择如何失败", params, seed, NODES,
        views=("cluster", "history"), auto_election=False,
        write_policy=WRITE_ANY_NODE if ap else WRITE_CONSENSUS,
        read_policy=READ_ANY_NODE if ap else READ_INDEX,
    )
    client = Client("客户端", kernel, trace, network, timeout=params["client_timeout"])

    trace.note(0,
               "五个副本保存同一个键 x。剧本固定：先正常写一次，再制造分区，"
               "两侧各写一次、各读一次，最后恢复。唯一的变量是分区期间允许谁服务。",
               level="phase", title="设定：一个键，两侧客户端")
    if ap:
        trace.note(0,
                   "AP 策略：任何副本都可以接受写入并立刻回复，写入随后尽力传播。"
                   "这是对最终一致系统的教学化近似，不含冲突合并策略。",
                   level="warn", title="策略 = AP")

    campaign(kernel, cluster, "A", 20)
    kernel.schedule_at(200, lambda: client.submit(put("op-1", "req-1", "x", 1), "A"))
    kernel.schedule_at(420, lambda: client.submit(read("op-2", "req-2", "x"), "A"))

    def cut() -> None:
        network.partition([["A", "B"], ["C", "D", "E"]])
        trace.note(kernel.now,
                   "网络裂成 {A B} 与 {C D E}。A 仍是任期 1 的 Leader，但它这一侧只有两票；"
                   "另一侧有三票，足以选出新的 Leader。",
                   level="phase", title="第二幕：分区")

    kernel.schedule_at(620, cut)
    campaign(kernel, cluster, "C", 700)
    kernel.schedule_at(950, lambda: client.submit(put("op-3", "req-3", "x", 2), "C"))

    def minority_write() -> None:
        client.submit(put("op-4", "req-4", "x", 3), "A")
        if ap:
            trace.note(kernel.now,
                       "少数派一侧照样接受 x=3 并回复 ok。此刻两侧对同一个键给出不同答案，"
                       "而两个客户端都收到了成功。",
                       level="warn", title="两侧同时可写")
        else:
            trace.note(kernel.now,
                       "少数派一侧的 A 接下了请求，却永远凑不齐多数派。"
                       "客户端最终只会拿到 unknown——注意它不是 fail。",
                       level="teach", title="拒绝把不确定说成成功")

    kernel.schedule_at(1250, minority_write)
    kernel.schedule_at(1600, lambda: client.submit(read("op-5", "req-5", "x"), "A"))
    kernel.schedule_at(1900, lambda: client.submit(read("op-6", "req-6", "x"), "C"))

    def heal() -> None:
        network.heal()
        trace.note(kernel.now,
                   "网络恢复。A 看到更高任期后退位，冲突后缀被截断。"
                   "注意：日志可以被截断，已经回复给用户的那个 ok 不能。",
                   level="phase", title="第三幕：恢复")

    kernel.schedule_at(2150, heal)
    kernel.schedule_at(2400, lambda: client.submit(read("op-7", "req-7", "x"), "A"))

    def verdict() -> None:
        if ap:
            trace.note(kernel.now,
                       "AP 的账单在这里：少数派那次已确认的写没有留在多数派上，"
                       "而读到的值序列无法排成任何合法的瞬时生效顺序。"
                       "检查器会给出最小反例。",
                       level="violation", title="AP 的代价")
        else:
            trace.note(kernel.now,
                       "CP 的账单在这里：分区期间少数派一侧的写与读都变成了 "
                       "unknown 或被拒绝，可用性下降；但没有任何承诺被推翻。",
                       level="teach", title="CP 的代价")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    trace.facts["final"] = cluster.snapshot()
    trace.facts["client"] = client.outcomes()
    checks = run_checks(trace, ("linearizable", "acked_not_lost", "committed_prefix_stable"))
    metrics = metrics_for(cluster, network, [client])
    metrics["策略 (policy)"] = params["policy"]
    metrics["客户端 fail"] = sum(1 for item in client.outcomes() if item.get("status") == "fail")
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="cap-partition",
    order=1,
    lecture="绪论",
    title="分区期间的选择",
    subtitle="CAP：不是三选二，是分区时如何失败",
    views=("cluster", "history"),
    params=PARAMS,
    run=run,
    question="网络分区期间，两侧都继续接受写入会怎样？只让多数派服务，代价又是什么？",
    takeaway="CAP 约束的是分区期间的请求保证，不是整个系统的开关。"
             "CP 把不能证明安全的请求变成 unknown 或拒绝；"
             "AP 保住了可用性，代价是历史不再线性一致，已确认的写也可能消失。",
    prediction=Prediction(
        question="分区期间两侧都接受写入，之后哪一条最先被打破？",
        options=("每个任期至多一个 Leader", "线性一致", "什么都不破，只是慢一点", "网络恢复后自动修复一切"),
        answer=1,
        why="两侧各自回复成功，客户端于是先读到 3、后读到 2。"
            "要让这段历史合法，必须存在一次把 x 写回 2 的操作——它并不存在。",
    ),
    invariants=("linearizable", "acked_not_lost", "committed_prefix_stable"),
    faults=("partition", "heal", "drop"),
    caveat="AP 一侧是教学化近似：只做日志尽力传播，不含向量时钟或冲突合并策略。",
))
