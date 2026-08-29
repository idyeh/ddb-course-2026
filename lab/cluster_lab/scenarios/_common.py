"""Helpers shared by the scenarios: cluster construction and scripted choreography."""

from __future__ import annotations

from typing import Any, Callable

from ..kernel import Kernel
from ..network import Network
from ..nodes.raft import LEADER, RaftCluster
from ..trace import Trace


def build(
    scenario_id: str,
    title: str,
    params: dict[str, Any],
    seed: int,
    node_ids: list[str],
    views: tuple[str, ...] = ("cluster",),
    auto_election: bool = True,
    **cluster_options: Any,
) -> tuple[Kernel, Trace, Network, RaftCluster]:
    trace = Trace(scenario_id, seed, params, list(views), title=title)
    kernel = Kernel(seed)
    network = Network(
        kernel,
        trace,
        delay_min=params.get("delay_min", 4),
        delay_max=params.get("delay_max", 18),
        drop=params.get("drop", 0.0),
    )
    cluster = RaftCluster(
        kernel,
        trace,
        network,
        node_ids,
        heartbeat=params.get("heartbeat", 40),
        timeout_min=params.get("timeout_min", 150),
        timeout_max=params.get("timeout_max", 300),
        auto_election=auto_election,
        **cluster_options,
    )
    return kernel, trace, network, cluster


def at(kernel: Kernel, when: int, action: Callable[[], None]) -> None:
    kernel.schedule_at(when, action)


def campaign(
    kernel: Kernel,
    cluster: RaftCluster,
    node_id: str,
    when: int,
    attempts: int = 3,
    gap: int = 90,
    force_first: bool = False,
) -> None:
    """Make `node_id` become leader, retrying if an older term loses the vote.

    A first attempt can lose because some voter already voted in a higher term.
    Retrying is what a real node does when its election timeout fires again;
    scripting it keeps the choreographed demonstrations reproducible.
    """

    def attempt(number: int) -> None:
        node = cluster.nodes[node_id]
        if not node.alive:
            return
        if node.state == LEADER and not (number == 0 and force_first):
            return
        cluster.force_election(node_id)

    for number in range(attempts):
        kernel.schedule_at(when + number * gap, lambda number=number: attempt(number))


def metrics_for(cluster: RaftCluster, network: Network, clients: list) -> dict[str, Any]:
    outcomes = [outcome for client in clients for outcome in client.outcomes()]
    leader = cluster.leader()
    return {
        "消息数 (messages)": network.sent,
        "丢弃数 (dropped)": network.dropped,
        "选举次数 (elections)": cluster.elections,
        "Leader 更替 (leader changes)": cluster.leader_changes,
        "客户端 ok": sum(1 for item in outcomes if item.get("status") == "ok"),
        "客户端 unknown": sum(1 for item in outcomes if item.get("status") == "info"),
        "最终 Leader": f"{leader.id} (term {leader.current_term})" if leader else "无",
    }
