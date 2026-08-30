"""第2讲：日志复制与提交规则 — 为什么不能只数旧任期条目的副本数。

`figure8` 模式是 Raft 论文 Figure 8 的受控编排 (scripted)：关闭自发选举超时，
按固定剧本推进，使两种提交规则的差别可以稳定复现。改一个开关，
`naive_majority` 会让一条已提交条目在后面被覆盖，`current_term` 则不会。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..nodes.client import Client
from ..nodes.kv import put
from ..nodes.raft import CURRENT_TERM, NAIVE_MAJORITY
from ..params import (DELAY_MAX, DELAY_MIN, DROP, HEARTBEAT, SEED, TIMEOUT_MAX,
                      TIMEOUT_MIN, Param, ParamSet)
from ..registry import Prediction, RunResult, Scenario, register
from ._common import build, campaign, metrics_for

PARAMS = ParamSet((
    Param("mode", "模式 (mode)", "choice", "figure8", choices=(
        ("figure8", "论文 Figure 8 编排：提交规则对比"),
        ("normal", "常规复制：前缀修复与追赶"),
    ), help="figure8 为受控编排，关闭自发选举超时以稳定复现。"),
    Param("commit_rule", "提交规则 (commit rule)", "choice", CURRENT_TERM, choices=(
        (CURRENT_TERM, "只用多数计数提交当前任期条目（Raft 规则）"),
        (NAIVE_MAJORITY, "任何条目达到多数即提交（错误规则）"),
    ), help="仅在 figure8 模式下产生差别。", axis=True),
    Param("writes", "写入次数 (writes)", "int", 6, minimum=1, maximum=20,
          help="仅用于常规复制模式。"),
    DROP, HEARTBEAT, TIMEOUT_MIN, TIMEOUT_MAX, DELAY_MIN, DELAY_MAX, SEED,
))

FIGURE8_NODES = ["S1", "S2", "S3", "S4", "S5"]
FIGURE8_END = 2400
NORMAL_NODES = ["A", "B", "C", "D", "E"]
NORMAL_END = 2400


def _figure8(params: dict[str, Any], seed: int) -> RunResult:
    kernel, trace, network, cluster = build(
        "raft-replication", "日志复制与提交规则", params, seed, FIGURE8_NODES,
        auto_election=False, commit_rule=params["commit_rule"], leader_noop=False,
    )
    client = Client("客户端", kernel, trace, network, timeout=250)
    naive = params["commit_rule"] == NAIVE_MAJORITY

    trace.note(0,
               "这是 Raft 论文 Figure 8 的受控编排：自发选举超时已关闭，"
               "每一次换届都由剧本触发，因此两种提交规则可以逐帧对照。"
               "唯一的变量是 Leader 用什么条件推进提交位置。",
               level="phase", title="设定：受控编排的五节点集群")

    campaign(kernel, cluster, "S1", 20)
    kernel.schedule_at(200, lambda: client.submit(put("op-1", "req-1", "x", 1), "S1"))
    campaign(kernel, cluster, "S1", 420, force_first=True)

    def act_a() -> None:
        network.partition([["S1", "S2"], ["S3", "S4", "S5"]])
        trace.note(kernel.now,
                   "S1 是任期 2 的 Leader。现在把它和 S2 一起隔离，"
                   "接下来那条写入只能复制到 S2。",
                   level="phase", title="(a) 索引 2 只到达两个副本")

    kernel.schedule_at(600, act_a)
    kernel.schedule_at(630, lambda: client.submit(put("op-2", "req-2", "x", 2), "S1"))
    kernel.schedule_at(800, lambda: cluster.crash("S1"))

    def act_b() -> None:
        trace.note(kernel.now,
                   "S1 崩溃。S5 拿到 S3、S4 的票当选任期 3 的 Leader——"
                   "它的日志里没有那条 term 2 的条目，但也没人要求它有："
                   "那条条目从未被提交。",
                   level="phase", title="(b) S5 当选任期 3")

    kernel.schedule_at(840, act_b)
    campaign(kernel, cluster, "S5", 850)

    def act_c() -> None:
        network.partition([["S1", "S2"], ["S3", "S4"], ["S5"]])
        trace.note(kernel.now,
                   "把 S5 也隔离，让它在索引 2 上写下一条属于任期 3 的条目。"
                   "现在索引 2 在集群里有两个互相冲突的版本。",
                   level="phase", title="(c) 索引 2 出现第二个版本")

    kernel.schedule_at(1100, act_c)
    kernel.schedule_at(1130, lambda: client.submit(put("op-3", "req-3", "y", 3), "S5"))
    kernel.schedule_at(1300, lambda: cluster.crash("S5"))

    def act_d() -> None:
        cluster.restart("S1")
        network.partition([["S1", "S2", "S3"], ["S4"], ["S5"]])
        trace.note(kernel.now,
                   "S5 崩溃，S1 重启。S1 第一次参选会失败——S3 已经在任期 3 投过票；"
                   "S1 从回复中得知更高任期，重试后以任期 4 当选。",
                   level="phase", title="(d) S1 重新当选任期 4")

    kernel.schedule_at(1350, act_d)
    campaign(kernel, cluster, "S1", 1400)

    def act_e() -> None:
        if naive:
            trace.note(kernel.now,
                       "S1 把那条 term 2 的旧条目复制给了 S3，索引 2 现在存在于 S1、S2、S3。"
                       "错误规则只数副本数：它认为索引 2 已经提交。"
                       "请记住这个提交标记——它马上就会被推翻。",
                       level="warn", title="(e) 错误规则在此提交了旧任期条目")
        else:
            trace.note(kernel.now,
                       "S1 同样把 term 2 的旧条目复制给了 S3，索引 2 也确实存在于三个副本上。"
                       "但 Raft 规则拒绝提交它：它不属于当前任期 4。"
                       "复制得多，不等于已经提交。",
                       level="teach", title="(e) Raft 规则拒绝提交旧任期条目")

    kernel.schedule_at(1700, act_e)

    def act_f() -> None:
        cluster.crash("S1")
        cluster.restart("S5")
        network.heal()
        trace.note(kernel.now,
                   "S1 再次崩溃，S5 回来了。S5 的最后一条日志是 term 3，"
                   "比 S2、S3 的 term 2 更新，因此它能赢得投票。",
                   level="phase", title="(f) S5 卷土重来")

    kernel.schedule_at(1800, act_f)
    campaign(kernel, cluster, "S5", 1900)

    def verdict() -> None:
        if naive:
            trace.note(kernel.now,
                       "S5 以任期 5 当选，把索引 2 覆盖成自己的 term 3 条目。"
                       "刚才被“提交”的那条记录消失了——客户端如果收到过成功，"
                       "那句成功现在没有任何依据。",
                       level="violation", title="(g) 已提交条目被覆盖")
        else:
            trace.note(kernel.now,
                       "S5 以任期 5 当选，同样覆盖了索引 2。区别在于："
                       "这条条目从来没有被宣布提交，所以没有任何承诺被推翻。"
                       "Raft 用一条限制换掉了这整类故障。",
                       level="teach", title="(g) 没有承诺被推翻")

    kernel.schedule_at(2150, verdict)
    kernel.run(FIGURE8_END)

    trace.facts["final"] = cluster.snapshot()
    trace.facts["client"] = client.outcomes()
    checks = run_checks(trace, ("committed_prefix_stable", "single_leader_per_term",
                                "at_most_once"))
    metrics = metrics_for(cluster, network, [client])
    metrics["提交规则 (commit rule)"] = params["commit_rule"]
    return RunResult(trace, checks, metrics)


def _normal(params: dict[str, Any], seed: int) -> RunResult:
    kernel, trace, network, cluster = build(
        "raft-replication", "日志复制与提交规则", params, seed, NORMAL_NODES,
        auto_election=True, commit_rule=params["commit_rule"],
    )
    client = Client("客户端", kernel, trace, network, timeout=300)

    trace.note(0,
               "常规复制：客户端把请求发给 A，不是 Leader 就按提示重定向。"
               "丢包会让某个 Follower 落后，Leader 通过回退 nextIndex 找到共同前缀，"
               "再补齐后缀——注意它修复的是前缀，而不是合并两边的命令。",
               level="phase", title="设定：五节点常规复制")

    for number in range(params["writes"]):
        when = 300 + number * 180
        command = put(f"op-{number + 1}", f"req-{number + 1}", "x", number + 1)
        kernel.schedule_at(when, lambda command=command: client.submit(command, "A"))

    def kill_leader() -> None:
        leader = cluster.leader()
        if leader is None:
            return
        cluster.crash(leader.id)
        trace.note(kernel.now,
                   f"Leader {leader.id} 崩溃。剩余节点超时后重新选举；"
                   "新 Leader 必须拥有足够新的日志才能赢得投票。",
                   level="phase", title="Leader 故障")

    kernel.schedule_at(int(NORMAL_END * 0.55), kill_leader)
    kernel.run(NORMAL_END)

    trace.facts["final"] = cluster.snapshot()
    trace.facts["client"] = client.outcomes()
    checks = run_checks(trace, ("committed_prefix_stable", "single_leader_per_term",
                                "acked_not_lost", "at_most_once", "replicas_agree"))
    metrics = metrics_for(cluster, network, [client])
    return RunResult(trace, checks, metrics)


def run(params: dict[str, Any], seed: int) -> RunResult:
    if params["mode"] == "figure8":
        return _figure8(params, seed)
    return _normal(params, seed)


register(Scenario(
    id="raft-replication",
    order=21,
    lecture="第2讲 复制与共识",
    title="日志复制与提交规则",
    subtitle="前缀修复，以及当前任期提交限制",
    views=("cluster",),
    params=PARAMS,
    run=run,
    question="一条旧任期的条目已经出现在多数副本的日志里，Leader 可以宣布它已提交吗？",
    takeaway="不可以。Leader 只能用多数计数直接提交当前任期的条目；"
             "当前任期条目一旦提交，它之前的前缀才随之提交。"
             "把“复制得多”当成“已经提交”，就会出现已提交条目被覆盖。",
    prediction=Prediction(
        question="索引 2 上那条 term 2 的条目被复制到 3/5 个副本后，会发生什么？",
        options=("两种规则都提交它", "两种规则都拒绝提交", "错误规则提交它，随后它被覆盖", "它会被立刻覆盖"),
        answer=2,
        why="错误规则只看副本数，于是提交了一条旧任期条目；"
            "之后 S5 凭借更新的最后一条日志赢得选举，把索引 2 覆盖掉，"
            "那条“已提交”记录随之消失。",
    ),
    invariants=("committed_prefix_stable", "single_leader_per_term", "at_most_once"),
    faults=("partition", "crash", "restart", "drop"),
    caveat="figure8 为受控编排：关闭自发选举超时、由剧本触发换届；"
           "同时关闭了新 Leader 的 noop 条目——论文 Figure 8 讨论的正是没有它的情形，"
           "而 noop 恰好是该问题的另一种解法（论文 5.4.2）。",
))
