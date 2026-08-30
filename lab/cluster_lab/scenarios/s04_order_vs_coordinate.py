"""第4讲：协调发生在执行中，还是执行之前？

同一批跨分片转账，三种做法。中途有一个分片崩溃并重启——
它会把三者的差别一次性暴露出来：谁保住了原子性，代价各是什么。
"""

from __future__ import annotations

from random import Random
from typing import Any

from ..invariants import run_checks
from ..kernel import Kernel
from ..network import Network
from ..nodes.ordering import (COORDINATE, DEPENDENT, KNOWN, NONE, PREORDER,
                              START_BALANCE, OrderingSystem)
from ..params import DELAY_MAX, DELAY_MIN, DROP, SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ..trace import Trace

PARAMS = ParamSet((
    Param("mode", "协调方式 (coordination)", "choice", COORDINATE, choices=(
        (COORDINATE, "执行中协调：每笔事务一次 2PC"),
        (PREORDER, "执行前定序：确定性调度"),
        (NONE, "不协调：两半各发各的"),
    ), axis=True),
    Param("rw_set", "读写集是否预先已知 (read/write set)", "choice", KNOWN, choices=(
        (KNOWN, "预先已知"),
        (DEPENDENT, "依赖读取结果，需要先侦察"),
    ), help="只影响执行前定序：Calvin 需要预知读写集。", axis=True),
    Param("txns", "事务数 (transactions)", "int", 12, minimum=4, maximum=40),
    Param("shard_crash", "中途崩溃一个分片", "choice", "on", choices=(
        ("on", "崩溃并在稍后重启"),
        ("off", "不注入故障"),
    )),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

SHARDS = ["D1", "D2", "D3", "D4"]
END = 2600
BATCH_EVERY = 260


def run(params: dict[str, Any], seed: int) -> RunResult:
    trace = Trace("order-vs-coordinate", seed, params, ["cluster"], title="定序还是协调")
    kernel = Kernel(seed)
    network = Network(kernel, trace, delay_min=params["delay_min"],
                      delay_max=params["delay_max"], drop=params["drop"])
    system = OrderingSystem(kernel, trace, network, SHARDS, params["mode"], params["rw_set"])
    trace.add_client("客户端")
    rng = Random(seed)

    trace.note(0,
               f"{len(SHARDS)} 个分片各有 {START_BALANCE}，客户端提交 "
               f"{params['txns']} 笔跨分片转账。总额必须守恒——"
               "这就是我们要保护的不变式。",
               level="phase", title="设定：跨分片转账")
    labels = {
        NONE: "不协调：客户端把两半分别发给两个分片，没有任何原子提交协议。",
        COORDINATE: "执行中协调：每笔事务都要经过 Prepare / Vote / Decide。",
        PREORDER: "执行前定序：Sequencer 先定好全局顺序并复制输入，"
                  "各分片再按同一份剧本本地执行。",
    }
    trace.note(0, labels[params["mode"]], level="teach", title="本次做法")
    if params["mode"] == NONE:
        trace.note(0,
                   "注意：不协调的做法连普通丢包都扛不住——丢掉一半，就是永久少了一半。"
                   "不需要崩溃也能看到总额对不上。",
                   level="warn", title="连丢包都扛不住")
    if params["mode"] == PREORDER and params["rw_set"] == DEPENDENT:
        trace.note(0,
                   "读写集依赖读取结果，因此每笔事务在定序之前还要先做一次侦察往返。"
                   "确定性调度的前提是“提前知道要碰哪些数据”。",
                   level="warn", title="需要侦察")

    for number in range(params["txns"]):
        source, target = rng.sample(SHARDS, 2)
        amount = rng.randrange(10, 90)
        kernel.schedule_at(
            200 + number * 120,
            lambda number=number, source=source, target=target, amount=amount:
            system.submit(f"T{number + 1}", source, target, amount))

    if params["mode"] == PREORDER:
        for tick in range(BATCH_EVERY, END, BATCH_EVERY):
            kernel.schedule_at(tick, system.seal_batch)

    if params["mode"] == COORDINATE:
        # A coordinator timeout sweep: retry decisions, give up on votes that never come.
        for tick in range(300, END, 220):
            kernel.schedule_at(tick, system.sweep)
    elif params["mode"] == PREORDER:
        for tick in range(400, END, 220):
            kernel.schedule_at(tick, system.resend_batches)

    victim = SHARDS[1]
    if params["shard_crash"] == "on":
        def crash() -> None:
            system.shards[victim].crash()
            if params["mode"] == COORDINATE:
                system.sweep()
            trace.note(kernel.now,
                       f"{victim} 崩溃。此刻正在进行的事务，有的已经改了一端。"
                       "接下来看每种做法怎么处理这个“做了一半”的局面。",
                       level="phase", title="第二幕：一个分片崩溃")

        kernel.schedule_at(1200, crash)
        kernel.schedule_at(1900, lambda: (
            system.shards[victim].restart(),
            trace.note(kernel.now, f"{victim} 重启。", level="phase", title="第三幕：恢复"),
        ))

    def verdict() -> None:
        total = system.total()
        listing = "，".join(f"{shard_id}={shard.balance}"
                            for shard_id, shard in sorted(system.shards.items()))
        expected = START_BALANCE * len(SHARDS)
        if total != expected:
            trace.note(kernel.now,
                       f"结束时总额 {total}，应为 {expected}（{listing}）。"
                       "没有原子提交协议，也没有可重放的输入日志，"
                       "崩溃时“只生效了一半”的事务就永久地留在了那里。",
                       level="violation", title="总额不守恒")
        else:
            trace.note(kernel.now,
                       f"结束时总额仍是 {total}（{listing}）。"
                       + ("2PC 在参与者失联时中止了受影响的事务，代价是每笔事务两轮往返。"
                          if params["mode"] == COORDINATE else
                          "确定性执行靠重放已复制的输入恢复，执行期间没有任何跨分片消息。"),
                       level="teach", title="总额守恒")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    trace.facts["final"] = system.snapshot()
    trace.facts["initial_total"] = START_BALANCE * len(SHARDS)
    trace.facts["final_total"] = system.total()

    checks = run_checks(trace, ("value_conserved",))
    cross_shard = sum(1 for event in trace.events
                      if event["type"] == "msg.send"
                      and event["kind"] in {"Prepare", "Vote", "Decide", "Ack"})
    metrics = {
        "协调方式 (mode)": params["mode"],
        "提交事务数": len(system.committed),
        "中止次数 (aborts)": system.aborts,
        "协调往返 (round trips)": system.round_trips,
        "侦察往返 (reconnaissance)": system.recon_trips,
        "执行期跨分片消息": cross_shard,
        "重发次数 (retransmits)": system.retransmits,
        "消息总数": network.sent,
        "总额 (期望 / 实际)": f"{START_BALANCE * len(SHARDS)} / {system.total()}",
    }
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="order-vs-coordinate",
    order=41,
    lecture="第4讲 事务与并发",
    title="定序还是协调",
    subtitle="Calvin 式预排序与 2PC 的对比",
    views=("cluster",),
    params=PARAMS,
    run=run,
    question="把协调从执行期间移到执行之前，能省下什么？又要额外付出什么？",
    takeaway="确定性事务并没有取消共识，它把协调移到了排序阶段："
             "输入被可靠复制之后，各分片按同一份剧本本地执行，执行期间不需要跨分片提交协议，"
             "恢复也变成重放同一份输入。代价是要预先知道读写集，"
             "排序层本身还会成为新的瓶颈。而完全不协调最省消息，也最先丢掉原子性。",
    prediction=Prediction(
        question="预先定序的做法在执行期间需要多少跨分片消息？",
        options=("和 2PC 差不多", "每笔事务一轮", "零——顺序已经定好了", "取决于冲突多少"),
        answer=2,
        why="所有分片拿到的是同一份已复制、已定序的输入，"
            "各自按同一顺序本地执行即可，执行期间不需要互相商量。"
            "协调没有消失，它被移到了排序与复制那一步。",
    ),
    invariants=("value_conserved",),
    faults=("crash", "restart", "drop"),
    caveat="三种做法都是教学化模型：单账户分片、无死锁检测；重点是协调发生在哪个阶段。",
))
