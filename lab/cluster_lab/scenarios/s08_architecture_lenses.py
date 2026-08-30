"""第8讲：同一笔跨分片读写事务，五条请求路径。

每种架构回答同样的五个问题：谁接收请求、谁分配顺序或时间戳、
哪些副本必须响应、冲突何时被发现、客户端收到成功之前还要等什么。
画不出这条路径，就还没有真正理解这个架构。
"""

from __future__ import annotations

from typing import Any

from ..kernel import Kernel
from ..network import Network
from ..params import DELAY_MAX, DELAY_MIN, SEED, Param, ParamSet
from ..registry import CheckResult, Prediction, RunResult, Scenario, register
from ..trace import Trace

CLIENT = "客户端"

ARCHITECTURES: dict[str, dict[str, Any]] = {
    "spanner": {
        "title": "Spanner 式：把时间不确定区间纳入提交协议",
        "nodes": [("CO", "coordinator"), ("P1", "participant"), ("P2", "participant"),
                  ("TT", "clock")],
        "order_by": "协调者选提交时间戳，并用 TrueTime 区间证明它已过去",
        "conflict": "Prepare 阶段各参与者本地检查锁与冲突",
        "wait": "commit wait：等到 TrueTime 确认该时刻已经过去",
        "async": "无（等待发生在回复之前）",
        "steps": [
            (60, "phase", "begin", "客户端提交事务", CLIENT, "CO", "BeginTxn"),
            (140, "phase", "prepare", "协调者向参与者发 Prepare（各自 Paxos 复制）",
             "CO", "P1", "Prepare"),
            (150, "msg", "CO", "P2", "Prepare"),
            (260, "msg", "P1", "CO", "Vote"),
            (270, "msg", "P2", "CO", "Vote"),
            (360, "phase", "timestamp", "协调者取 TT.now()，在区间上界之后选提交时间戳",
             "CO", "TT", "TT.now()"),
            (440, "msg", "TT", "CO", "[earliest, latest]"),
            (520, "phase", "commit_wait",
             "commit wait：等到 TT.after(s) 为真，才敢说这个时刻已经过去", "CO", "CO", "等待"),
            (760, "phase", "commit", "下发提交", "CO", "P1", "Commit"),
            (770, "msg", "CO", "P2", "Commit"),
            (880, "phase", "reply", "回复客户端成功", "CO", CLIENT, "ok"),
        ],
    },
    "cockroachdb": {
        "title": "CockroachDB 式：Range + Raft + HLC + 事务记录",
        "nodes": [("GW", "coordinator"), ("R1", "participant"), ("R2", "participant"),
                  ("TXN", "record")],
        "order_by": "网关用 HLC 给事务分配时间戳，每个 Range 由自己的 Raft 组定序",
        "conflict": "写意向落在 Range 上时发现；读遇到未决意向要等待或推进",
        "wait": "写意向与事务记录都被各自的 Raft 组复制成功",
        "async": "有：意向解析（intent resolution）在回复之后进行",
        "steps": [
            (60, "phase", "begin", "客户端连到网关节点", CLIENT, "GW", "BEGIN"),
            (140, "phase", "intents", "网关把写意向写入各 Range（每个 Range 一个 Raft 组）",
             "GW", "R1", "写意向"),
            (150, "msg", "GW", "R2", "写意向"),
            (170, "phase", "record", "同时写事务记录", "GW", "TXN", "事务记录"),
            (300, "msg", "R1", "GW", "已复制"),
            (310, "msg", "R2", "GW", "已复制"),
            (320, "msg", "TXN", "GW", "已复制"),
            (420, "phase", "reply", "意向与记录都已复制，事务即可视为提交，回复客户端",
             "GW", CLIENT, "ok"),
            (560, "phase", "resolve", "异步解析意向：这一步发生在客户端已经拿到成功之后",
             "GW", "R1", "解析意向"),
            (570, "msg", "GW", "R2", "解析意向"),
        ],
    },
    "calvin": {
        "title": "Calvin 式：先定序，再按同一份剧本执行",
        "nodes": [("SEQ", "coordinator"), ("REP", "record"), ("S1", "participant"),
                  ("S2", "participant")],
        "order_by": "Sequencer 为一批事务确定全局顺序",
        "conflict": "由确定性顺序消解：同一顺序在每个分片上产生同样的加锁次序",
        "wait": "事务输入被可靠复制（这一步仍然是共识）",
        "async": "无跨分片提交协议；执行阶段各分片互不通信",
        "steps": [
            (60, "phase", "begin", "客户端把事务交给 Sequencer", CLIENT, "SEQ", "提交事务"),
            (150, "phase", "replicate", "先复制输入——确定性并没有取消共识",
             "SEQ", "REP", "复制批次"),
            (280, "msg", "REP", "SEQ", "多数派确认"),
            (380, "phase", "execute", "各分片按同一顺序本地执行，执行期间不互相通信",
             "SEQ", "S1", "执行批次"),
            (390, "msg", "SEQ", "S2", "执行批次"),
            (520, "msg", "S1", "SEQ", "完成"),
            (530, "msg", "S2", "SEQ", "完成"),
            (620, "phase", "reply", "回复客户端成功", "SEQ", CLIENT, "ok"),
        ],
    },
    "foundationdb": {
        "title": "FoundationDB 式：窄事务内核 + 乐观并发控制",
        "nodes": [("PROXY", "coordinator"), ("SEQ", "clock"), ("RES", "record"),
                  ("LOG", "participant")],
        "order_by": "Sequencer 分配读版本与提交版本",
        "conflict": "提交时由 Resolver 校验读写冲突范围（乐观）",
        "wait": "冲突校验通过，且写入被事务日志持久化",
        "async": "存储服务器随后从日志异步拉取更新",
        "steps": [
            (60, "phase", "begin", "客户端取读版本", CLIENT, "PROXY", "读版本?"),
            (120, "msg", "PROXY", "SEQ", "读版本?"),
            (200, "msg", "SEQ", "PROXY", "read version"),
            (270, "msg", "PROXY", CLIENT, "read version"),
            (350, "phase", "buffer", "读在客户端建立冲突范围，写先缓存在客户端",
             CLIENT, CLIENT, "本地缓存写集"),
            (430, "phase", "resolve", "提交时把读写范围交给 Resolver 校验",
             CLIENT, "PROXY", "COMMIT"),
            (500, "msg", "PROXY", "RES", "冲突校验"),
            (610, "msg", "RES", "PROXY", "无冲突"),
            (690, "phase", "persist", "写入事务日志并等待持久化", "PROXY", "LOG", "持久化"),
            (800, "msg", "LOG", "PROXY", "已持久化"),
            (880, "phase", "reply", "回复客户端成功", "PROXY", CLIENT, "ok"),
        ],
    },
    "tidb": {
        "title": "TiDB 式：TSO + Percolator 风格的两阶段提交",
        "nodes": [("TiDB", "coordinator"), ("PD", "clock"), ("K1", "participant"),
                  ("K2", "participant")],
        "order_by": "PD 的 TSO 分配 start_ts 与 commit_ts",
        "conflict": "prewrite 阶段在各 Region 上检查锁与写写冲突",
        "wait": "prewrite 全部成功，且主键 (primary key) 的 commit 已写下",
        "async": "有：次要键的 commit 在回复之后异步完成",
        "steps": [
            (60, "phase", "begin", "客户端连到 TiDB Server", CLIENT, "TiDB", "BEGIN"),
            (130, "phase", "start_ts", "向 PD 取 start_ts", "TiDB", "PD", "start_ts?"),
            (210, "msg", "PD", "TiDB", "start_ts"),
            (290, "phase", "prewrite", "向各 Region prewrite（主键 K1，次要键 K2）",
             "TiDB", "K1", "prewrite 主键"),
            (300, "msg", "TiDB", "K2", "prewrite 次要键"),
            (430, "msg", "K1", "TiDB", "ok"),
            (440, "msg", "K2", "TiDB", "ok"),
            (520, "phase", "commit_ts", "再向 PD 取 commit_ts", "TiDB", "PD", "commit_ts?"),
            (600, "msg", "PD", "TiDB", "commit_ts"),
            (680, "phase", "commit_primary",
             "提交主键——**事务在这一刻已经提交**", "TiDB", "K1", "commit 主键"),
            (790, "msg", "K1", "TiDB", "ok"),
            (860, "phase", "reply", "回复客户端成功", "TiDB", CLIENT, "ok"),
            (940, "phase", "commit_secondary", "异步提交次要键", "TiDB", "K2", "commit 次要键"),
        ],
    },
}

PARAMS = ParamSet((
    Param("architecture", "架构 (architecture)", "choice", "spanner", choices=(
        ("spanner", "Spanner 式"),
        ("cockroachdb", "CockroachDB 式"),
        ("calvin", "Calvin 式"),
        ("foundationdb", "FoundationDB 式"),
        ("tidb", "TiDB 式"),
    ), axis=True),
    DELAY_MIN, DELAY_MAX, SEED,
))

END = 1120


def run(params: dict[str, Any], seed: int) -> RunResult:
    spec = ARCHITECTURES[params["architecture"]]
    trace = Trace("architecture-lenses", seed, params, ["cluster"], title="架构镜头")
    kernel = Kernel(seed)
    network = Network(kernel, trace, delay_min=params["delay_min"],
                      delay_max=params["delay_max"], drop=0.0)

    for node_id, kind in spec["nodes"]:
        trace.add_node(node_id, kind=kind)
        state = kind  # the palette renders each component kind on its own
        trace.record(0, "node.state", node=node_id, state=state, term=0, commit=0,
                     votedFor=None, alive=True, detail="", reason="组件", lastIndex=0)
    trace.add_client(CLIENT)
    network.attach(CLIENT, lambda source, message: None)
    for node_id, _ in spec["nodes"]:
        network.attach(node_id, lambda source, message: None)

    trace.note(0,
               f"{spec['title']}。同一笔跨两个分片的读写事务，"
               "请盯住四件事：谁分配顺序、哪些组件必须响应、冲突何时被发现、"
               "客户端拿到成功之前还在等什么。",
               level="phase", title="设定：一笔跨分片事务")
    trace.note(0,
               "这是**教学化的路径模型**，用来比较协调发生在哪里、等待发生在哪里。"
               "它不是任何产品的实测行为，也不能用来比较性能。",
               level="warn", title="模型边界")

    phases: list[str] = []
    for step in spec["steps"]:
        at = step[0]
        if step[1] == "phase":
            _, _, name, label, source, target, message_label = step
            phases.append(name)

            def emit(name=name, label=label, source=source, target=target,
                     message_label=message_label) -> None:
                trace.record(kernel.now, "arch.phase", name=name, label=label)
                trace.note(kernel.now, label, level="teach", title=f"阶段：{name}")
                if source != target:
                    network.send(source, target, {"type": message_label}, label=message_label)

            kernel.schedule_at(at, emit)
        else:
            _, _, source, target, message_label = step
            kernel.schedule_at(at, lambda source=source, target=target,
                               message_label=message_label:
                               network.send(source, target, {"type": message_label},
                                            label=message_label))

    reply_at = next(step[0] for step in spec["steps"]
                    if step[1] == "phase" and step[2] == "reply")
    kernel.schedule_at(reply_at - 1, lambda: trace.record(
        kernel.now, "client.call", client=CLIENT, opId="T1", reqId="T1",
        op="跨分片读写事务", kind="txn", key=None, value=None, target=spec["nodes"][0][0]))
    kernel.schedule_at(reply_at + 30, lambda: trace.record(
        kernel.now, "client.return", client=CLIENT, opId="T1", reqId="T1",
        op="跨分片读写事务", kind="txn", status="ok", calledAt=reply_at - 1))

    kernel.schedule_at(END - 40, lambda: trace.note(
        kernel.now,
        f"顺序由谁分配：{spec['order_by']}。冲突何时发现：{spec['conflict']}。"
        f"成功之前还要等：{spec['wait']}。成功之后的异步工作：{spec['async']}。",
        level="phase", title="四个答案"))
    kernel.run(END)

    trace.facts["phases"] = phases
    trace.facts["reply_at"] = reply_at

    checks = [_path_complete(trace, phases)]
    critical = sum(1 for step in spec["steps"] if step[0] <= reply_at
                   and step[1] in {"phase", "msg"})
    metrics = {
        "架构 (architecture)": params["architecture"],
        "顺序 / 时间戳由谁分配": spec["order_by"],
        "冲突何时被发现": spec["conflict"],
        "成功之前还要等什么": spec["wait"],
        "成功之后的异步工作": spec["async"],
        "关键路径上的步骤数": critical,
        "关键路径耗时 (ticks)": reply_at,
    }
    return RunResult(trace, checks, metrics)


def _path_complete(trace: Trace, phases: list[str]) -> CheckResult:
    """Every declared phase must actually appear, in order, around the reply."""
    name = "请求路径完整 (declared path is walked)"
    seen = [event["name"] for event in trace.events if event["type"] == "arch.phase"]
    if seen != phases:
        return CheckResult(name, False, f"声明的阶段是 {phases}，实际走过的是 {seen}。")
    reply_index = phases.index("reply")
    before = phases[:reply_index]
    reply_at = trace.facts.get("reply_at", 0)
    late = [event["name"] for event in trace.events
            if event["type"] == "arch.phase" and event["name"] in before
            and event["t"] > reply_at]
    if late:
        return CheckResult(name, False, f"{late} 出现在回复客户端之后，路径描述与实际不符。")
    return CheckResult(name, True,
                       f"共 {len(phases)} 个阶段，其中 {reply_index} 个在回复客户端之前完成。")


register(Scenario(
    id="architecture-lenses",
    order=80,
    lecture="第8讲 架构比较",
    title="架构镜头",
    subtitle="同一笔事务的五条请求路径",
    views=("cluster",),
    params=PARAMS,
    run=run,
    question="任选一个系统，画出跨两个分片的读写事务：谁接收请求？谁分配顺序？"
             "哪些副本必须响应？冲突何时发现？客户端收到成功前还要等待什么？",
    takeaway="系统名称会变，比较方法更耐久：保证 → 顺序 → 分片与复制 → 请求路径 → 验证。"
             "五个系统的差别不在“支持 SQL、水平扩展、高可用”，"
             "而在协调与等待被安排在路径的哪一段——"
             "Spanner 把等待放进提交、Calvin 把协调提到执行之前、"
             "TiDB 与 CockroachDB 把一部分工作挪到回复之后。",
    prediction=Prediction(
        question="哪一种架构在客户端拿到成功之后仍有必须完成的异步工作？",
        options=("Spanner 式", "Calvin 式", "TiDB 式与 CockroachDB 式", "都没有"),
        answer=2,
        why="TiDB 提交主键后即可回复，次要键异步提交；"
            "CockroachDB 在意向与事务记录复制成功后即可回复，意向解析随后进行。"
            "Spanner 的等待（commit wait）发生在回复之前，Calvin 的协调发生在执行之前。",
    ),
    invariants=("请求路径完整",),
    faults=(),
    caveat="教学化的路径模型：只比较协调与等待发生在路径的哪一段，"
           "不代表任何产品的实测行为，也不能用于性能比较。相关官方文档见 sources.md。",
))
