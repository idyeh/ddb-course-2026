"""第6讲：快照安装与追赶 — “进程已启动”不等于“副本可以对外服务”。

一个副本掉队足够久，Leader 需要的那段日志前缀已经被压缩掉了。
它只能先传一份绑定了 lastIncludedIndex / Term 的快照，再继续复制后续日志。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..nodes.client import Client
from ..nodes.kv import put, read
from ..nodes.raft import READ_FOLLOWER
from ..params import DELAY_MAX, DELAY_MIN, DROP, SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ._common import build, campaign, metrics_for

PARAMS = ParamSet((
    Param("writes", "落后期间的写入数 (writes)", "int", 6, minimum=2, maximum=12),
    Param("snapshot_threshold", "压缩阈值 (compaction threshold)", "int", 3,
          minimum=0, maximum=20,
          help="已应用条目超过该数量即压缩；设为 0 表示从不压缩，落后副本便可只靠日志追赶。",
          axis=True),
    Param("client_timeout", "客户端超时 (client timeout)", "int", 600, minimum=100, maximum=2000),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

NODES = ["A", "B", "C", "D", "E"]
END = 2600


def run(params: dict[str, Any], seed: int) -> RunResult:
    kernel, trace, network, cluster = build(
        "snapshot-catchup", "快照安装与追赶", params, seed, NODES,
        views=("cluster", "history"), auto_election=False,
        read_policy=READ_FOLLOWER, snapshot_threshold=params["snapshot_threshold"],
    )
    client = Client("客户端", kernel, trace, network, timeout=params["client_timeout"])

    trace.note(0,
               "E 会被隔离一段时间，其余四个副本继续接收写入。"
               "注意 Leader 的日志矩阵：应用位置每超过压缩阈值，前缀就被换成一份快照。",
               level="phase", title="设定：让一个副本掉队")

    campaign(kernel, cluster, "A", 20)

    def isolate() -> None:
        network.partition([["A", "B", "C", "D"], ["E"]])
        trace.note(kernel.now, "E 被隔离。它仍然活着，只是什么也收不到。",
                   level="phase", title="第二幕：E 掉队")

    kernel.schedule_at(150, isolate)

    last_value = params["writes"]
    for number in range(params["writes"]):
        when = 300 + number * 160
        command = put(f"op-{number + 1}", f"req-{number + 1}", "x", number + 1)
        kernel.schedule_at(when, lambda command=command: client.submit(command, "A"))

    def compacted_note() -> None:
        leader = cluster.leader()
        base = leader.log_base if leader else 0
        if base:
            trace.note(kernel.now,
                       f"Leader 已把索引 ≤ {base} 的日志压缩进快照。"
                       "快照本身必须绑定 lastIncludedIndex 与 Term，否则后续日志接不上。",
                       level="teach", title="日志被压缩")
        else:
            trace.note(kernel.now,
                       "压缩阈值为 0，Leader 保留了完整日志。掉队副本可以只靠日志追赶，"
                       "代价是日志无限增长。",
                       level="teach", title="没有压缩")

    kernel.schedule_at(1330, compacted_note)

    def heal() -> None:
        network.heal()
        trace.note(kernel.now,
                   "网络恢复。Leader 想发给 E 的那段前缀已经不在日志里了，"
                   "于是改发 InstallSnapshot；装完再用 AppendEntries 补后面的尾巴。",
                   level="phase", title="第三幕：快照安装")

    kernel.schedule_at(1400, heal)

    def read_too_early() -> None:
        client.submit(read("op-read-1", "req-read-1", "x"), "E")
        trace.note(kernel.now,
                   "就在此刻向 E 发一次强一致读。E 是活的，但它还没追上——"
                   "Follower Read 会先向 Leader 取 ReadIndex，再等自己应用到那个位置才回答。"
                   "看历史条：这次读会一直等到追赶完成。",
                   level="teach", title="进程已启动 ≠ 可以服务")

    kernel.schedule_at(1420, read_too_early)
    kernel.schedule_at(2100, lambda: client.submit(read("op-read-2", "req-read-2", "x"), "E"))

    def verdict() -> None:
        node = cluster.nodes["E"]
        trace.note(kernel.now,
                   f"E 现在的快照位置是 {node.log_base}，应用位置 {node.last_applied}，"
                   f"状态 {node.machine.summary() or '空'}。"
                   "快照 + 后续日志必须连续，去重表也要一起恢复，否则重试会重复生效。",
                   level="phase", title="对账")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    trace.facts["final"] = cluster.snapshot()
    trace.facts["client"] = client.outcomes()
    checks = run_checks(trace, ("linearizable", "acked_not_lost", "committed_prefix_stable",
                                "replicas_agree", "log_structure_sound"))
    metrics = metrics_for(cluster, network, [client])
    installs = sum(1 for event in trace.events
                   if event["type"] == "log.snapshot" and event.get("installed"))
    metrics["快照安装次数 (InstallSnapshot)"] = installs
    metrics["E 的快照位置"] = cluster.nodes["E"].log_base
    metrics["最后写入的值"] = last_value
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="snapshot-catchup",
    order=60,
    lecture="第6讲 快照与恢复",
    title="快照安装与追赶",
    subtitle="压缩日志，但不丢失协议位置",
    views=("cluster", "history"),
    params=PARAMS,
    run=run,
    question="一个副本掉队太久，Leader 已经把它需要的日志压缩掉了，接下来怎么办？它追上之前能提供读取吗？",
    takeaway="快照是状态传输，必须绑定 lastIncludedIndex/Term，并与后续日志连续；"
             "去重表等协议状态也要一起进快照。副本恢复连接只是第一步，"
             "它必须先追上提交位置，才有资格回答强一致读。",
    prediction=Prediction(
        question="刚恢复连接、尚未追上的 E 收到一次强一致读，会怎样？",
        options=("立刻返回它本地的旧值", "先等自己追上再回答", "直接报错", "转发给 Leader 由 Leader 回答"),
        answer=1,
        why="Follower Read 先向 Leader 取一个 ReadIndex，再等本地应用位置到达那里。"
            "它宁可等待，也不返回一个无法证明的旧值——这正是它与陈旧读的区别。",
    ),
    invariants=("linearizable", "acked_not_lost", "committed_prefix_stable",
                "replicas_agree", "log_structure_sound"),
    faults=("partition", "heal", "drop"),
))
