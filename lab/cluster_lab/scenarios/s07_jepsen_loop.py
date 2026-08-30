"""第7讲：生成、注入、记录、检查 — 一个完整的验证回路。

前面的演示都是编排好的剧本。这一个不是：它随机生成操作、随机注入故障，
然后**搜索**多个种子，直到找出一条不合法的历史，再把它缩到最小。
找不到反例不等于没有反例——这一点会写在结论里。
"""

from __future__ import annotations

from random import Random
from typing import Any

from ..checkers.linearizable import MAX_OPERATIONS
from ..invariants import run_checks
from ..nodes.client import Client
from ..nodes.kv import put, read
from ..nodes.raft import READ_FOLLOWER, READ_INDEX, READ_LEADER_ONLY
from ..params import DELAY_MAX, DELAY_MIN, DROP, SEED, Param, ParamSet
from ..registry import RunResult, Scenario, Prediction, register
from ._common import build, campaign, metrics_for

PARAMS = ParamSet((
    Param("read", "被测读协议 (read protocol)", "choice", READ_LEADER_ONLY, choices=(
        (READ_LEADER_ONLY, "直接从 Leader 读"),
        (READ_INDEX, "ReadIndex"),
        (READ_FOLLOWER, "Follower Read"),
    ), axis=True),
    Param("nemesis", "故障注入 (nemesis)", "choice", "partition", choices=(
        ("partition", "随机分区并恢复"),
        ("crash", "随机崩溃并重启"),
        ("none", "不注入故障"),
    ), axis=True),
    Param("ops", "每轮操作数 (operations)", "int", 9, minimum=4, maximum=MAX_OPERATIONS),
    Param("seeds", "搜索多少个种子 (seeds)", "int", 12, minimum=1, maximum=40,
          help="从起始种子开始逐个尝试，找到第一条反例就停下。"),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

NODES = ["A", "B", "C", "D", "E"]
END = 2600


def _one_run(params: dict[str, Any], seed: int):
    kernel, trace, network, cluster = build(
        "jepsen-loop", "验证回路", params, seed, NODES,
        views=("history", "cluster"), auto_election=False, read_policy=params["read"],
    )
    client = Client("客户端", kernel, trace, network, timeout=280)
    rng = Random(seed * 7919 + 13)

    campaign(kernel, cluster, "A", 20)
    trace.note(0,
               "生成器随机产生读写操作，nemesis 随机制造故障，"
               "全部结果（包括超时）进入历史，最后交给线性一致检查器。"
               "这四个部件缺一不可。",
               level="phase", title="设定：一个验证回路")

    # Generator: unique written values make each read traceable to its source.
    value = [0]
    for number in range(params["ops"]):
        at = 260 + number * 210
        target = rng.choice(NODES)
        if rng.random() < 0.45:
            value[0] += 1
            command = put(f"op-{number + 1}", f"req-{number + 1}", "x", value[0])
        else:
            command = read(f"op-{number + 1}", f"req-{number + 1}", "x")
        kernel.schedule_at(at, lambda command=command, target=target:
                           client.submit(command, target))

    # Nemesis: a good one attacks the assumption on purpose. Isolating the current
    # leader is what creates the situation a stale read needs.
    if params["nemesis"] != "none":
        for round_number in range(2):
            start = 500 + round_number * 900

            def strike(round_number=round_number) -> None:
                leader = cluster.leader()
                if leader is None:
                    return
                partner = rng.choice([node for node in NODES if node != leader.id])
                minority = [leader.id, partner]
                majority = [node for node in NODES if node not in minority]
                if params["nemesis"] == "partition":
                    network.partition([minority, majority])
                    trace.record(kernel.now, "nemesis", kind="partition",
                                 isolated=sorted(minority))
                else:
                    cluster.crash(leader.id)
                    trace.record(kernel.now, "nemesis", kind="crash", node=leader.id)
                campaign(kernel, cluster, majority[0], kernel.now + 40)

            def heal(round_number=round_number) -> None:
                if params["nemesis"] == "partition":
                    network.heal()
                else:
                    for node_id in NODES:
                        cluster.restart(node_id)
                trace.record(kernel.now, "nemesis", kind="heal")

            kernel.schedule_at(start, strike)
            kernel.schedule_at(start + 700, heal)

    kernel.run(END)
    trace.facts["final"] = cluster.snapshot()
    trace.facts["client"] = client.outcomes()
    checks = run_checks(trace, ("linearizable", "at_most_once"))
    return trace, checks, cluster, network, client


def run(params: dict[str, Any], seed: int) -> RunResult:
    searched = 0
    found = None
    last = None
    for candidate in range(seed, seed + params["seeds"]):
        searched += 1
        last = _one_run(params, candidate)
        if any(not check.ok for check in last[1]):
            found = (candidate, last)
            break

    if found:
        failing_seed, (trace, checks, cluster, network, client) = found
        failing = next(check for check in checks if not check.ok)
        trace.note(END,
                   f"第 {searched} 个种子（seed={failing_seed}）给出了反例。"
                   f"{failing.detail} 请把这个种子记进回归测试——"
                   "它现在是一个可以随时重放的失败用例。",
                   level="violation", title="找到反例")
    else:
        trace, checks, cluster, network, client = last
        failing_seed = None
        trace.note(END,
                   f"{searched} 个种子都没有发现反例。这**不是**一个正确性证明："
                   "固定种子提供重放，不提供覆盖率。要提高把握，"
                   "应当扩大种子范围、加强生成器，或改用小模型枚举与形式化方法。",
                   level="warn", title="没找到，不等于没有")

    metrics = metrics_for(cluster, network, [client])
    history = [item for item in client.outcomes()]
    metrics["搜索的种子数"] = searched
    metrics["反例种子"] = failing_seed if failing_seed is not None else "未找到"
    metrics["历史操作数"] = len(history)
    counterexample = next((check.counterexample for check in checks
                           if not check.ok and check.counterexample), [])
    minimal = len({event.get("opId") for event in counterexample if event.get("opId")})
    metrics["最小反例操作数"] = minimal or "—"
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="jepsen-loop",
    order=70,
    lecture="第7讲 验证与可靠性",
    title="验证回路",
    subtitle="生成器、nemesis、历史与检查器",
    views=("history", "cluster"),
    params=PARAMS,
    run=run,
    question="怎样从客户端外部证明一个系统违反了它声称的保证？找不到反例又能说明什么？",
    takeaway="一个验证回路由四个部件组成：生成可追踪的操作、注入破坏假设的故障、"
             "完整记录历史（包括 unknown）、用检查器判定模型。"
             "找到反例时，最有价值的产物是**种子加最小历史**；"
             "没找到反例时，唯一诚实的结论是“这些调度里没找到”，而不是“系统正确”。",
    prediction=Prediction(
        question="连续跑了 12 个随机种子都没有发现反例。可以得出什么结论？",
        options=("系统满足线性一致", "这些调度里没有发现反例",
                 "再多跑一小时就能证明", "检查器有问题"),
        answer=1,
        why="随机搜索扩大的是调度覆盖，不是证明。"
            "固定种子提供重放能力；要得到更强的结论，需要小模型枚举或形式化验证。",
    ),
    invariants=("linearizable", "at_most_once"),
    faults=("partition", "crash", "restart", "drop"),
    caveat="检查器是穷举搜索，因此每轮操作数被限制在 14 以内；这是课堂规模，不是压力测试。",
))
