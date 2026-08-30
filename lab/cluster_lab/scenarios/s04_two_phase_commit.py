"""第4讲：两阶段提交 — prepared 是一项义务，不是一次暂存。

参与者投出赞成票的那一刻就承担了义务：持久化 prepared、持有锁、等待最终决议。
它不能自行超时回滚。给协调者的决议日志加上复制，可以缩短等待，
但决议仍然必须送达参与者——“用了 Raft”并不能自动推出“非阻塞”。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..kernel import Kernel
from ..network import Network
from ..nodes.client import Client
from ..nodes.kv import txn
from ..nodes.txn import (CRASH_AFTER_DECISION, CRASH_BEFORE_DECISION, CRASH_NONE,
                         REPLICATED, SINGLE, TwoPhaseSystem)
from ..params import DELAY_MAX, DELAY_MIN, DROP, SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ..trace import Trace

PARAMS = ParamSet((
    Param("crash_point", "协调者崩溃时机 (crash point)", "choice", CRASH_AFTER_DECISION, choices=(
        (CRASH_NONE, "不崩溃"),
        (CRASH_AFTER_DECISION, "写下决议之后、通知之前"),
        (CRASH_BEFORE_DECISION, "收齐投票之后、写下决议之前"),
    ), axis=True),
    Param("coordinator_log", "决议日志 (coordinator log)", "choice", SINGLE, choices=(
        (SINGLE, "单副本：只有原协调者能读"),
        (REPLICATED, "复制：备用协调者也能读"),
    ), help="复制的是决议日志，不是决定权。", axis=True),
    Param("recovery", "是否恢复 (recovery)", "choice", "on", choices=(
        ("on", "协调者最终恢复"),
        ("off", "协调者一直不回来"),
    ), help="关掉它，就能看到 prepared 参与者一直等下去。"),
    Param("client_timeout", "客户端超时 (client timeout)", "int", 400, minimum=120, maximum=2000),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

PARTICIPANTS = ["P1", "P2", "P3"]
END = 3200


def run(params: dict[str, Any], seed: int) -> RunResult:
    trace = Trace("two-phase-commit", seed, params, ["cluster", "history"],
                  title="两阶段提交")
    kernel = Kernel(seed)
    network = Network(kernel, trace, delay_min=params["delay_min"],
                      delay_max=params["delay_max"], drop=params["drop"])
    system = TwoPhaseSystem(kernel, trace, network, PARTICIPANTS,
                            coordinator_log=params["coordinator_log"],
                            crash_point=params["crash_point"])
    client = Client("客户端", kernel, trace, network, timeout=params["client_timeout"])

    replicated = params["coordinator_log"] == REPLICATED
    recovering = params["recovery"] == "on"
    recovery_at = 1300 if replicated else 2100
    second_txn_at = (recovery_at + 350) if recovering else 1600

    trace.note(0,
               "一笔事务同时写 P1 的 a 和 P2 的 b。协调者先收集投票，再持久化决议，"
               "最后通知参与者。请盯住参与者的状态：prepared 意味着它已经持锁，"
               "并且失去了自行了结的权利。",
               level="phase", title="设定：跨两个分片的事务")
    if replicated:
        trace.note(0,
                   "决议日志已复制到备用协调者 C2。注意它复制的是**已经写下的决议**，"
                   "而不是做决定的权力。",
                   level="teach", title="决议日志被复制")

    kernel.schedule_at(200, lambda: client.submit(
        txn("T1", "req-T1", {"P1": {"a": 1}, "P2": {"b": 1}}), "C1"))

    def after_prepare() -> None:
        blocked = system.blocked()
        if blocked:
            trace.note(kernel.now,
                       f"{'、'.join(blocked)} 停在 prepared：已经持久化、正在持锁、"
                       "并周期性地向协调者询问决议。它们不能自行超时回滚——"
                       "万一决议是 commit，擅自回滚就会撕裂这笔事务。",
                       level="warn", title="prepared 参与者只能等")

    kernel.schedule_at(700, after_prepare)

    def recover() -> None:
        if not recovering:
            return
        if replicated:
            trace.note(kernel.now,
                       "备用协调者 C2 接管。它读到的是同一份决议日志："
                       "有决议就补发，没有决议就可以安全地回滚——因为没有决议，"
                       "就不可能有人被告知提交过。",
                       level="phase", title="备用协调者接管")
            system.promote_backup()
        else:
            trace.note(kernel.now,
                       "原协调者重启，从自己的持久决议日志继续。等待时间取决于它什么时候回来——"
                       "这正是单副本决议日志的代价。",
                       level="phase", title="原协调者返回")
            system.restart_coordinator()

    kernel.schedule_at(recovery_at, recover)

    kernel.schedule_at(second_txn_at, lambda: client.submit(
        txn("T2", "req-T2", {"P1": {"a": 2}}), "C1" if not replicated or not recovering else "C2"))

    def second_note() -> None:
        blocked = system.blocked()
        if blocked:
            trace.note(kernel.now,
                       "第二笔事务想写同一个键，却撞上仍未释放的锁。"
                       "阻塞的代价不只落在原事务身上，它会扩散到后面的负载。",
                       level="warn", title="阻塞会扩散")

    kernel.schedule_at(second_txn_at + 120, second_note)

    def verdict() -> None:
        blocked = system.blocked()
        if blocked:
            trace.note(kernel.now,
                       f"运行结束时 {'、'.join(blocked)} 仍停在 prepared。"
                       "注意这属于活性 (liveness) 问题，不是安全性问题："
                       "没有任何承诺被推翻，只是这笔事务一直没有了结。"
                       "安全性检查因此仍然全部通过。",
                       level="warn", title="阻塞是活性代价，不是安全性违反")
        else:
            decisions = "，".join(f"{key}={value}"
                                 for key, value in sorted(system.log.decisions.items()))
            trace.note(kernel.now,
                       f"所有事务都已了结：{decisions}。"
                       "客户端那边未必这么干净——超时返回的 unknown 可能对应一笔已经提交的事务。",
                       level="phase", title="对账")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    trace.facts["final"] = system.snapshot()
    trace.facts["client"] = client.outcomes()
    checks = run_checks(trace, ("atomic_commit", "no_unilateral_resolution"))
    outcomes = {item["opId"]: item.get("status") for item in client.outcomes()}
    metrics = {
        "消息数 (messages)": network.sent,
        "丢弃数 (dropped)": network.dropped,
        "持久化的决议": "，".join(f"{key}={value}"
                             for key, value in sorted(system.log.decisions.items())) or "无",
        "结束时仍 prepared（活性代价）": "、".join(system.blocked()) or "无",
        "T1 客户端结果": outcomes.get("T1", "未返回"),
        "T2 客户端结果": outcomes.get("T2", "未返回"),
        "T1 阻塞时长 (ticks)": _blocked_ticks(trace, "T1"),
    }
    return RunResult(trace, checks, metrics)


def _blocked_ticks(trace: Trace, txn_id: str) -> str:
    """From the first yes vote to the moment the transaction was finally resolved."""
    prepared = next((event["t"] for event in trace.events
                     if event["type"] == "txn.vote" and event["txn"] == txn_id and event["ok"]), None)
    resolved = next((event["t"] for event in trace.events
                     if event["type"] == "txn.state" and event["txn"] == txn_id), None)
    if prepared is None:
        return "未进入 prepared"
    if resolved is None:
        return f"≥ {trace.duration - prepared}（仍未了结）"
    return str(resolved - prepared)


register(Scenario(
    id="two-phase-commit",
    order=40,
    lecture="第4讲 事务与并发",
    title="两阶段提交",
    subtitle="prepared 状态、决议持久化与阻塞",
    views=("cluster", "history"),
    params=PARAMS,
    run=run,
    question="协调者在写下决议之后崩溃，参与者能不能自己超时回滚？给决议日志加上复制，2PC 就非阻塞了吗？",
    takeaway="prepared 是协议义务：参与者已持久化、正在持锁，只能等最终决议。"
             "复制决议日志让新协调者可以恢复已记录的决议，缩短等待；"
             "但决议仍必须送达参与者，是否非阻塞取决于完整的提交协议，"
             "不能由“用了 Raft”直接推出。",
    prediction=Prediction(
        question="协调者持久化 commit 之后立刻崩溃，客户端会看到什么？",
        options=("事务回滚，客户端看到失败", "客户端超时得到 unknown，事务其实已提交",
                 "参与者超时后自行回滚", "客户端立刻看到成功"),
        answer=1,
        why="决议已经写下，事务注定提交；但通知没发出去，客户端只能超时。"
            "这正是课程强调的：COMMIT 超时不等于回滚，应用必须能查询事务状态或使用幂等编号。",
    ),
    invariants=("atomic_commit", "no_unilateral_resolution"),
    faults=("crash", "restart", "drop", "delay"),
    caveat="参与者与协调者是教学模型：单事务锁、无死锁检测，重点在 prepared 义务与决议持久化。",
))
