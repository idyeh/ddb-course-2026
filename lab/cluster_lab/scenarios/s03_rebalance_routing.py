"""第3讲：分裂、迁移与过期路由 — 路由错误必须是协议内可恢复的状态。

客户端缓存的范围映射一定会过期。正确的做法是让持有方回答“这个范围已经搬走了，
线索在这里”，由客户端刷新后重试；如果旧分片将错就错地服务了请求，
写入就落在了一个不再拥有该键的分片上，随后的读再也看不到它。
"""

from __future__ import annotations

from typing import Any

from ..invariants import run_checks
from ..kernel import Kernel
from ..network import Network
from ..nodes.client import Client
from ..nodes.kv import put, read
from ..nodes.routing import ACCEPT_STALE, COMPOSITE, REJECT_STALE, ShardSystem
from ..params import DELAY_MAX, DELAY_MIN, DROP, SEED, Param, ParamSet
from ..registry import Prediction, RunResult, Scenario, register
from ..trace import Trace

PARAMS = ParamSet((
    Param("stale_route", "旧分片如何处理过期路由 (stale route)", "choice", REJECT_STALE, choices=(
        (REJECT_STALE, "回答“范围已迁移”，让客户端刷新重试"),
        (ACCEPT_STALE, "将错就错，照样服务"),
    ), axis=True),
    Param("client_timeout", "客户端超时 (client timeout)", "int", 300, minimum=100, maximum=1200),
    DROP, DELAY_MIN, DELAY_MAX, SEED,
))

SHARDS = ["S0", "S1", "S2", "S3"]
KEY = "tenant-1/k2"
END = 2400


def run(params: dict[str, Any], seed: int) -> RunResult:
    trace = Trace("rebalance-routing", seed, params, ["shards", "history"],
                  title="分裂、迁移与过期路由")
    kernel = Kernel(seed)
    network = Network(kernel, trace, delay_min=params["delay_min"],
                      delay_max=params["delay_max"], drop=params["drop"])
    system = ShardSystem(kernel, trace, network, SHARDS, COMPOSITE, "tenant-1",
                         stale_policy=params["stale_route"])
    client = Client("客户端", kernel, trace, network, timeout=params["client_timeout"])

    home = system.owner_of(KEY)
    destination = next(shard for shard in SHARDS if shard != home)
    stale_target = home  # what the client's cached table still says

    trace.note(0,
               f"键 {KEY} 现在属于 {home}。客户端把这个映射缓存了下来——"
               "接下来范围会分裂并迁移，缓存随之过期。过期本身不是问题，"
               "问题是过期之后系统怎么回应。",
               level="phase", title="设定：客户端缓存了路由")

    kernel.schedule_at(150, lambda: client.submit(put("op-1", "req-1", KEY, 1), stale_target))
    kernel.schedule_at(450, lambda: client.submit(read("op-2", "req-2", KEY), stale_target))

    def split() -> None:
        system.split([KEY], destination)
        trace.note(kernel.now,
                   f"范围分裂：{KEY} 从 {home} 迁移到 {destination}，路由表版本变成 "
                   f"{system.table.version}。客户端还不知道，它的缓存仍指向 {home}。",
                   level="phase", title="第二幕：分裂与迁移")

    kernel.schedule_at(750, split)

    def stale_write() -> None:
        client.submit(put("op-3", "req-3", KEY, 2), stale_target)
        if params["stale_route"] == REJECT_STALE:
            trace.note(kernel.now,
                       f"客户端按旧映射把写入发给了 {home}。{home} 不再拥有这个键，"
                       "于是回答“范围已迁移”并附上线索；客户端刷新元数据后重试。",
                       level="teach", title="过期路由是可恢复状态")
        else:
            trace.note(kernel.now,
                       f"客户端按旧映射把写入发给了 {home}，而 {home} 将错就错地执行了它。"
                       f"这条写入落在了一个不再拥有该键的分片上——{destination} 对此一无所知。",
                       level="warn", title="将错就错")

    kernel.schedule_at(1050, stale_write)
    kernel.schedule_at(1500, lambda: client.submit(read("op-4", "req-4", KEY), destination))
    kernel.schedule_at(1900, lambda: client.submit(read("op-5", "req-5", KEY), destination))

    def verdict() -> None:
        values = {shard_id: node.data.get(KEY) for shard_id, node in system.nodes.items()}
        listing = "，".join(f"{shard}={value}" for shard, value in sorted(values.items())
                           if value is not None)
        if params["stale_route"] == ACCEPT_STALE:
            trace.note(kernel.now,
                       f"运行结束时 {KEY} 在多个分片上有不同的值：{listing}。"
                       "客户端明明收到过 ok，随后从正确的分片读却看不到它。"
                       "把路由错误当成普通成功处理，代价就是这个。",
                       level="violation", title="写入落错了地方")
        else:
            trace.note(kernel.now,
                       f"运行结束时只有所有者持有这个键：{listing}。"
                       "客户端经历了一次重定向和一次重试，但从头到尾没有丢失或看错任何数据。",
                       level="teach", title="刷新元数据后重试")

    kernel.schedule_at(END - 60, verdict)
    kernel.run(END)

    trace.facts["final"] = system.snapshot()
    trace.facts["client"] = client.outcomes()
    redirects = sum(1 for event in trace.events if event["type"] == "client.redirect")
    checks = run_checks(trace, ("routed_correctly", "linearizable"))
    metrics = {
        "消息数 (messages)": network.sent,
        "键的原始所有者": home,
        "迁移目标": destination,
        "重定向次数 (redirects)": redirects,
        "路由表版本": system.table.version,
        "各分片持有的值": "，".join(
            f"{shard}={node.data.get(KEY)}" for shard, node in sorted(system.nodes.items())
            if node.data.get(KEY) is not None) or "无",
    }
    return RunResult(trace, checks, metrics)


register(Scenario(
    id="rebalance-routing",
    order=31,
    lecture="第3讲 分区与分片",
    title="分裂、迁移与过期路由",
    subtitle="路由错误必须可恢复",
    views=("shards", "history"),
    params=PARAMS,
    run=run,
    question="客户端缓存的范围映射过期了，请求发到了旧分片。旧分片该怎么回答？",
    takeaway="客户端缓存的路由一定会过期，这是分片系统的常态而非异常。"
             "正确做法是让旧持有方回答“范围已迁移”并给出线索，由客户端刷新元数据后重试——"
             "路由错误是协议内可恢复的状态，不是不可解释的服务器错误，"
             "更不能当成一次普通的成功来处理。",
    prediction=Prediction(
        question="旧分片收到一个它已不再拥有的键的写入，直接执行会怎样？",
        options=("没关系，数据最终会同步过去", "写入落在错误的分片上，之后从正确分片读不到它",
                 "客户端会收到错误", "分裂会自动回滚"),
        answer=1,
        why="迁移已经完成，旧分片不再是这个键的所有者，也没有任何机制把它写下的值送到新所有者那里。"
            "客户端收到了 ok，随后却读不到自己刚写的值。",
    ),
    invariants=("routed_correctly", "linearizable"),
    faults=("split", "stale_route", "drop"),
))
