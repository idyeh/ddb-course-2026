"""Tests for the lab.

The negative tests matter as much as the positive ones: a demonstration that can
never fail does not demonstrate anything. Each deliberately broken configuration
below is required to produce its counterexample.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from cluster_lab import registry, server  # noqa: E402
from cluster_lab.checkers import linearizable as lin  # noqa: E402
from cluster_lab.kernel import Kernel  # noqa: E402
from cluster_lab.params import Param, ParamSet  # noqa: E402

SEEDS = (2026, 7, 101, 4242, 90210)


def run(scenario_id: str, **overrides) -> dict:
    return server.run_scenario(scenario_id, overrides)


def violations(payload: dict) -> set[str]:
    return set(payload["summary"]["violations"])


class KernelTests(unittest.TestCase):
    def test_events_fire_in_tick_then_insertion_order(self) -> None:
        kernel = Kernel(seed=1)
        seen: list[str] = []
        kernel.schedule(5, lambda: seen.append("b"))
        kernel.schedule(5, lambda: seen.append("c"))
        kernel.schedule(1, lambda: seen.append("a"))
        kernel.run(10)
        self.assertEqual(seen, ["a", "b", "c"])
        self.assertEqual(kernel.now, 10)

    def test_cannot_schedule_into_the_past(self) -> None:
        kernel = Kernel(seed=1)
        kernel.schedule(3, lambda: None)
        kernel.run(3)
        with self.assertRaises(ValueError):
            kernel.schedule_at(1, lambda: None)


class ParamTests(unittest.TestCase):
    def test_resolve_fills_defaults_and_drops_unknown_keys(self) -> None:
        params = ParamSet((Param("n", "n", "int", 3, minimum=1, maximum=5),))
        self.assertEqual(params.resolve({"other": 9}), {"n": 3})
        self.assertEqual(params.resolve({"n": "4"}), {"n": 4})

    def test_out_of_range_and_unknown_choice_are_rejected(self) -> None:
        params = ParamSet((
            Param("n", "n", "int", 3, minimum=1, maximum=5),
            Param("mode", "mode", "choice", "a", choices=(("a", "A"), ("b", "B"))),
        ))
        with self.assertRaises(ValueError):
            params.resolve({"n": 99})
        with self.assertRaises(ValueError):
            params.resolve({"mode": "z"})


class TraceContractTests(unittest.TestCase):
    """The trace is the contract with the browser; keep it well formed."""

    def test_every_scenario_produces_a_well_formed_trace(self) -> None:
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                payload = run(scenario.id)
                self.assertEqual(payload["schema"], 1)
                self.assertTrue(payload["events"])
                last_tick, last_seq = -1, 0
                for event in payload["events"]:
                    self.assertGreaterEqual(event["t"], last_tick)
                    self.assertGreater(event["seq"], last_seq)
                    self.assertIn("type", event)
                    last_tick, last_seq = event["t"], event["seq"]

    def test_every_message_is_delivered_or_dropped_exactly_once(self) -> None:
        """The contract the README states: no message is left hanging at the horizon."""
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                payload = run(scenario.id)
                sent, ended = set(), []
                for event in payload["events"]:
                    if event["type"] == "msg.send":
                        self.assertNotIn(event["id"], sent, "duplicate send id")
                        sent.add(event["id"])
                    elif event["type"] in {"msg.deliver", "msg.drop"}:
                        ended.append(event["id"])
                self.assertEqual(len(ended), len(set(ended)), "a message ended twice")
                self.assertEqual(set(ended), sent,
                                 f"unresolved at the horizon: {sorted(sent - set(ended))}")

    def test_messages_still_travelling_at_the_horizon_are_named_as_such(self) -> None:
        payload = run("raft-election")
        closing = [event for event in payload["events"]
                   if event["type"] == "msg.drop" and event.get("cause") == "simulation_end"]
        self.assertTrue(closing, "this scenario should end with heartbeats in flight")
        for event in closing:
            self.assertEqual(event["t"], payload["duration"])

    def test_declared_views_match_the_views_the_trace_emits(self) -> None:
        """The menu promises tabs; the trace decides which ones the UI can render."""
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                self.assertEqual(list(scenario.views), run(scenario.id)["views"])

    def test_every_check_reports_one_of_three_states(self) -> None:
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                summary = run(scenario.id)["summary"]
                for check in summary["checks"]:
                    self.assertIn(check["status"], {"pass", "fail", "inconclusive"})
                    if check["status"] == "fail":
                        self.assertIn(check["name"], summary["violations"])
                    if check["status"] == "inconclusive":
                        self.assertIn(check["name"], summary["inconclusive"])
                        self.assertNotIn(check["name"], summary["violations"])

    def test_declared_invariants_all_appear_in_the_summary(self) -> None:
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                names = {check["name"] for check in run(scenario.id)["summary"]["checks"]}
                self.assertTrue(names, f"{scenario.id} reported no checks")


class DeterminismTests(unittest.TestCase):
    def test_the_same_seed_replays_byte_for_byte(self) -> None:
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                first = json.dumps(run(scenario.id, seed=2026), sort_keys=True)
                second = json.dumps(run(scenario.id, seed=2026), sort_keys=True)
                self.assertEqual(first, second)

    def test_a_different_seed_gives_a_different_trace(self) -> None:
        first = json.dumps(run("raft-election", seed=2026), sort_keys=True)
        second = json.dumps(run("raft-election", seed=2027), sort_keys=True)
        self.assertNotEqual(first, second)


class SafeConfigurationTests(unittest.TestCase):
    """Correct settings must hold their invariants across several declared seeds."""

    def test_ack_meaning_is_clean_with_majority_ack_and_stable_request_ids(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                payload = run("ack-meaning", ack="majority", retry="same_id", seed=seed)
                self.assertEqual(violations(payload), set())

    def test_figure8_holds_under_the_current_term_commit_rule(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                payload = run("raft-replication", mode="figure8",
                              commit_rule="current_term", seed=seed)
                self.assertEqual(violations(payload), set())

    def test_normal_replication_holds(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                payload = run("raft-replication", mode="normal", seed=seed)
                self.assertEqual(violations(payload), set())

    def test_election_safety_survives_partitions(self) -> None:
        for seed in SEEDS:
            for partition in ("none", "leader_alone", "minority"):
                with self.subTest(seed=seed, partition=partition):
                    payload = run("raft-election", partition=partition, seed=seed)
                    self.assertEqual(violations(payload), set())


class CounterexampleTests(unittest.TestCase):
    """A demonstration that cannot fail proves nothing. These must fail."""

    def test_local_ack_loses_a_write_that_was_answered_ok(self) -> None:
        payload = run("ack-meaning", ack="local", retry="same_id")
        self.assertIn("已确认写不丢失 (acked writes survive)", violations(payload))
        failing = next(check for check in payload["summary"]["checks"] if not check["ok"])
        self.assertTrue(failing["counterexample"], "a violation must point at a history")

    def test_a_fresh_request_id_on_retry_applies_the_operation_twice(self) -> None:
        payload = run("ack-meaning", ack="majority", retry="new_id")
        self.assertIn("同一操作至多生效一次 (at most once)", violations(payload))
        self.assertEqual(payload["summary"]["metrics"]["最终计数器 (counter)"], 4)
        self.assertEqual(payload["summary"]["metrics"]["业务操作数 (logical ops)"], 4)

    def test_naive_majority_commit_loses_a_committed_entry(self) -> None:
        payload = run("raft-replication", mode="figure8", commit_rule="naive_majority")
        self.assertIn("已提交前缀不被推翻 (committed prefix stable)", violations(payload))

    def test_both_commit_rules_end_with_the_same_logs(self) -> None:
        """Only the commit marker differs: that is the whole teaching point."""
        logs = {}
        for rule in ("current_term", "naive_majority"):
            payload = run("raft-replication", mode="figure8", commit_rule=rule)
            logs[rule] = {node: state["log"] for node, state in payload["facts"]["final"].items()}
        self.assertEqual(logs["current_term"], logs["naive_majority"])


class LinearizabilityCheckerTests(unittest.TestCase):
    """The checker must agree with the course module wherever that one applies."""

    @staticmethod
    def _histories(count: int = 40):
        from random import Random

        rng = Random(2026)
        for _ in range(count):
            clock, ops = 0, []
            for number in range(rng.randint(3, 6)):
                call = clock + rng.randint(0, 2)
                ret = call + rng.randint(1, 3)
                clock = ret - rng.randint(0, 1)
                if rng.random() < 0.5:
                    ops.append(lin.Op(f"o{number}", "write", rng.randrange(3), call, ret))
                else:
                    ops.append(lin.Op(f"o{number}", "read", rng.randrange(3), call, ret))
            yield ops

    def test_agrees_with_the_course_checker_when_everything_returned(self) -> None:
        from examples.history_checker import Operation, is_linearizable as course_check

        for ops in self._histories():
            course = [
                Operation(op.id, op.kind, op.value if op.kind == "write" else None,
                          op.value if op.kind == "read" else None, op.call, op.ret)
                for op in ops
            ]
            self.assertEqual(
                lin.is_linearizable(ops, initial=0), course_check(course, initial=0),
                msg=f"disagreement on {[op.describe() for op in ops]}",
            )

    def test_an_unknown_write_may_simply_never_have_happened(self) -> None:
        ops = [lin.Op("w", "write", 1, 0, None), lin.Op("r", "read", None, 2, 4)]
        self.assertTrue(lin.is_linearizable(ops))

    def test_a_stale_read_after_a_completed_write_is_illegal(self) -> None:
        ops = [
            lin.Op("w1", "write", 1, 0, 2),
            lin.Op("w2", "write", 2, 3, 5),
            lin.Op("r", "read", 1, 6, 8),
        ]
        self.assertFalse(lin.is_linearizable(ops))

    def test_shrinking_never_removes_the_write_a_surviving_read_needs(self) -> None:
        ops = [
            lin.Op("w1", "write", 1, 0, 2),
            lin.Op("w2", "write", 2, 3, 5),
            lin.Op("w3", "write", 3, 6, 8),
            lin.Op("r1", "read", 1, 9, 11),
            lin.Op("r2", "read", 2, 12, 14),
        ]
        minimal = lin.shrink(ops)
        self.assertFalse(lin.is_linearizable(minimal))
        values = {op.value for op in minimal if op.kind == "write"} | {None}
        for op in minimal:
            if op.kind == "read":
                self.assertIn(op.value, values, "shrinking invented an unexplainable read")


class ReadPathTests(unittest.TestCase):
    def test_reading_from_a_stale_leader_without_proof_breaks_linearizability(self) -> None:
        payload = run("read-path", read="leader_only")
        self.assertIn("线性一致 (linearizable)", violations(payload))
        stale = next(item for item in payload["facts"]["client"] if item["opId"] == "op-3")
        self.assertEqual((stale["status"], stale["result"]), ("ok", 1))

    def test_readindex_refuses_to_answer_instead_of_answering_stale(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                payload = run("read-path", read="readindex", seed=seed)
                self.assertEqual(violations(payload), set())

    def test_a_lease_is_safe_while_its_clock_assumption_holds(self) -> None:
        safe = run("read-path", read="lease", clock_skew="off")
        self.assertEqual(violations(safe), set())
        skewed = run("read-path", read="lease", clock_skew="on")
        self.assertIn("线性一致 (linearizable)", violations(skewed))

    def test_a_follower_can_serve_a_strongly_consistent_read(self) -> None:
        payload = run("read-path", read="follower_read")
        self.assertEqual(violations(payload), set())
        served = next(item for item in payload["facts"]["client"] if item["opId"] == "op-4")
        self.assertEqual((served["status"], served["result"], served["served_by"]), ("ok", 2, "E"))


class PartitionPolicyTests(unittest.TestCase):
    def test_choosing_consistency_keeps_every_invariant(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.assertEqual(violations(run("cap-partition", policy="cp_majority", seed=seed)),
                                 set())

    def test_accepting_writes_on_both_sides_breaks_the_history(self) -> None:
        payload = run("cap-partition", policy="ap_any_node")
        self.assertIn("线性一致 (linearizable)", violations(payload))
        self.assertIn("已确认写不丢失 (acked writes survive)", violations(payload))
        failing = next(check for check in payload["summary"]["checks"]
                       if check["name"] == "线性一致 (linearizable)")
        self.assertTrue(failing["counterexample"])


class SnapshotSafetyTests(unittest.TestCase):
    """Regression coverage for a stale AppendEntries arriving after compaction."""

    @staticmethod
    def _compacted_follower():
        from cluster_lab.kernel import Kernel
        from cluster_lab.network import Network
        from cluster_lab.nodes.kv import put
        from cluster_lab.nodes.raft import Entry, RaftCluster
        from cluster_lab.trace import Trace

        trace = Trace("unit", 1, {}, ["cluster"])
        kernel = Kernel(seed=1)
        network = Network(kernel, trace)
        cluster = RaftCluster(kernel, trace, network, ["A", "B"], auto_election=False)
        node = cluster.nodes["B"]
        node.current_term = 3
        for index in range(1, 5):
            node.log.append(Entry(3, index, put(f"op-{index}", f"req-{index}", "x", index)))
        node.commit_index = node.last_applied = 4
        node._apply_committed()
        node.compact(3)
        return kernel, node

    def test_a_stale_append_entries_cannot_write_inside_the_snapshot(self) -> None:
        kernel, node = self._compacted_follower()
        self.assertEqual(node.log_base, 3)
        before = node.machine.summary()

        # An AppendEntries from the same term, delayed long enough that it still
        # describes a prefix this replica has already compacted away.
        node.receive("A", {
            "type": "AppendEntries", "term": 3, "leader": "A",
            "prevLogIndex": 0, "prevLogTerm": 0, "leaderCommit": 0, "readId": None,
            "entries": [
                {"term": 3, "index": index,
                 "command": {"kind": "set", "key": "x", "value": index * 10,
                             "opId": f"stale-{index}", "reqId": f"stale-{index}"}}
                for index in (1, 2)
            ],
        })

        self.assertEqual(node.log_base, 3, "the snapshot position must not move")
        self.assertTrue(all(entry.index > node.log_base for entry in node.log),
                        f"log holds entries inside the snapshot: "
                        f"{[entry.index for entry in node.log]}")
        self.assertEqual([entry.index for entry in node.log],
                         list(range(node.log_base + 1, node.last_index + 1)))
        self.assertEqual(node.last_index, 4)
        self.assertEqual(node.commit_index, 4)
        self.assertEqual(node.machine.summary(), before, "state must be untouched")

    def test_the_reply_tells_the_leader_where_the_snapshot_ends(self) -> None:
        kernel, node = self._compacted_follower()
        sent: list[dict] = []
        node.cluster.network.send = lambda source, target, message, label="": sent.append(message)
        node.receive("A", {
            "type": "AppendEntries", "term": 3, "leader": "A", "prevLogIndex": 0,
            "prevLogTerm": 0, "leaderCommit": 0, "entries": [], "readId": None,
        })
        reply = sent[-1]
        self.assertFalse(reply["success"])
        self.assertEqual(reply["compactedThrough"], 3)

    def test_entries_overlapping_the_snapshot_boundary_are_skipped_not_rewritten(self) -> None:
        kernel, node = self._compacted_follower()
        node.receive("A", {
            "type": "AppendEntries", "term": 3, "leader": "A",
            "prevLogIndex": 3, "prevLogTerm": node.snapshot_term, "leaderCommit": 4,
            "readId": None,
            "entries": [
                {"term": 3, "index": 4,
                 "command": {"kind": "set", "key": "x", "value": 4,
                             "opId": "op-4", "reqId": "req-4"}},
                {"term": 3, "index": 5,
                 "command": {"kind": "set", "key": "x", "value": 5,
                             "opId": "op-5", "reqId": "req-5"}},
            ],
        })
        self.assertEqual([entry.index for entry in node.log], [4, 5])
        self.assertEqual(node.log_base, 3)

    def test_snapshot_catchup_survives_a_lossy_slow_network(self) -> None:
        """The configuration from the review: high loss and long, variable delays."""
        for seed in (25, 2026, 7, 101, 4242):
            with self.subTest(seed=seed):
                payload = run("snapshot-catchup", seed=seed, drop=0.5,
                              delay_min=100, delay_max=200)
                self.assertNotIn("日志结构自洽 (log structure sound)", violations(payload))
                for node, state in payload["facts"]["final"].items():
                    indexes = [entry["index"] for entry in state["log"]]
                    self.assertTrue(all(index > state["base"] for index in indexes),
                                    f"{node}: base={state['base']} log={indexes}")


class SnapshotTests(unittest.TestCase):
    def test_a_lagging_replica_is_caught_up_by_a_snapshot(self) -> None:
        payload = run("snapshot-catchup", snapshot_threshold=3)
        self.assertEqual(violations(payload), set())
        self.assertGreaterEqual(payload["summary"]["metrics"]["快照安装次数 (InstallSnapshot)"], 1)
        self.assertEqual(payload["facts"]["final"]["E"]["machine"],
                         payload["facts"]["final"]["A"]["machine"])

    def test_without_compaction_no_snapshot_is_ever_sent(self) -> None:
        payload = run("snapshot-catchup", snapshot_threshold=0)
        self.assertEqual(payload["summary"]["metrics"]["快照安装次数 (InstallSnapshot)"], 0)
        self.assertEqual(violations(payload), set())

    def test_a_replica_that_has_not_caught_up_waits_instead_of_answering_stale(self) -> None:
        """The read may time out while catching up; what it must never do is answer stale."""
        for seed in SEEDS:
            with self.subTest(seed=seed):
                payload = run("snapshot-catchup", seed=seed)
                latest = payload["summary"]["metrics"]["最后写入的值"]
                early = next(item for item in payload["facts"]["client"]
                             if item["opId"] == "op-read-1")
                self.assertIn(early["status"], {"ok", "info"})
                if early["status"] == "ok":
                    self.assertEqual(early["result"], latest)

    def test_the_early_read_does_catch_up_on_the_declared_seed(self) -> None:
        payload = run("snapshot-catchup", seed=2026)
        early = next(item for item in payload["facts"]["client"] if item["opId"] == "op-read-1")
        self.assertEqual((early["status"], early["result"]),
                         ("ok", payload["summary"]["metrics"]["最后写入的值"]))


class TwoPhaseCommitTests(unittest.TestCase):
    CONFIGURATIONS = [
        {"crash_point": crash, "coordinator_log": log, "recovery": recovery}
        for crash in ("none", "after_decision", "before_decision")
        for log in ("single", "replicated")
        for recovery in ("on", "off")
    ]

    def test_safety_holds_in_every_configuration(self) -> None:
        for options in self.CONFIGURATIONS:
            for seed in (2026, 7):
                with self.subTest(seed=seed, **options):
                    self.assertEqual(violations(run("two-phase-commit", seed=seed, **options)),
                                     set())

    def test_a_commit_timeout_does_not_mean_the_transaction_rolled_back(self) -> None:
        payload = run("two-phase-commit", crash_point="after_decision")
        metrics = payload["summary"]["metrics"]
        self.assertEqual(metrics["T1 客户端结果"], "info")
        self.assertIn("T1=committed", metrics["持久化的决议"])

    def test_blocking_is_a_liveness_cost_not_a_safety_violation(self) -> None:
        payload = run("two-phase-commit", crash_point="after_decision", recovery="off")
        self.assertEqual(violations(payload), set())
        self.assertNotEqual(payload["summary"]["metrics"]["结束时仍 prepared（活性代价）"], "无")

    def test_a_replicated_decision_log_shortens_the_block_but_does_not_remove_it(self) -> None:
        single = run("two-phase-commit", crash_point="after_decision", coordinator_log="single")
        replicated = run("two-phase-commit", crash_point="after_decision",
                         coordinator_log="replicated")
        blocked = [int(payload["summary"]["metrics"]["T1 阻塞时长 (ticks)"])
                   for payload in (single, replicated)]
        self.assertGreater(blocked[0], blocked[1])
        self.assertGreater(blocked[1], 0, "even a replicated log leaves a blocking window")

    def test_a_participant_never_resolves_without_a_persisted_decision(self) -> None:
        for options in self.CONFIGURATIONS:
            payload = run("two-phase-commit", **options)
            decided = {event["txn"]: event["t"] for event in payload["events"]
                       if event["type"] == "txn.decision"}
            for event in payload["events"]:
                if event["type"] == "txn.state":
                    self.assertIn(event["txn"], decided)
                    self.assertLessEqual(decided[event["txn"]], event["t"])


class ClockTests(unittest.TestCase):
    """The clock table from the lecture, turned into assertions."""

    EXPECTED = {
        # (clock, skew): (causality preserved, concurrency inferred correctly)
        ("physical", "on"): (False, False),
        ("physical", "off"): (True, False),
        ("lamport", "on"): (True, False),
        ("lamport", "off"): (True, False),
        ("vector", "on"): (True, True),
        ("vector", "off"): (True, True),
        ("hlc", "on"): (True, False),
        ("hlc", "off"): (True, False),
    }

    def test_each_clock_answers_exactly_the_questions_it_can(self) -> None:
        for (clock, skew), expected in self.EXPECTED.items():
            with self.subTest(clock=clock, skew=skew):
                checks = run("four-clocks", clock=clock, skew=skew)["summary"]["checks"]
                self.assertEqual((checks[0]["ok"], checks[1]["ok"]), expected)

    def test_only_the_vector_clock_identifies_concurrency(self) -> None:
        winners = [clock for clock in ("physical", "lamport", "vector", "hlc")
                   if run("four-clocks", clock=clock)["summary"]["checks"][1]["ok"]]
        self.assertEqual(winners, ["vector"])

    def test_lamport_stamps_follow_the_course_module_rule(self) -> None:
        """a → b must imply C(a) < C(b); that is all Lamport promises."""
        payload = run("four-clocks", clock="lamport")
        events = {event["id"]: event for event in payload["events"]
                  if event["type"] == "tl.event"}
        by_process: dict[str, list] = {}
        for event in events.values():
            by_process.setdefault(event["proc"], []).append(event)
        for series in by_process.values():
            for first, second in zip(series, series[1:]):
                self.assertLess(first["lamport"], second["lamport"])


class DeterministicApplyTests(unittest.TestCase):
    def test_deciding_before_ordering_keeps_replicas_identical(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                payload = run("nondeterministic-apply", mode="predecided", seed=seed)
                self.assertEqual(violations(payload), set())
                self.assertEqual(payload["summary"]["metrics"]["不同状态数 (distinct states)"], 1)

    def test_identical_logs_still_produce_different_states(self) -> None:
        payload = run("nondeterministic-apply", mode="at_apply")
        self.assertIn("相同日志得到相同状态 (deterministic apply)", violations(payload))

        final = payload["facts"]["final"]
        logs = {node: [(entry["index"], entry["term"], entry["opId"]) for entry in state["log"]]
                for node, state in final.items()}
        self.assertEqual(len(set(map(tuple, logs.values()))), 1,
                         "the whole point is that the logs are identical")
        self.assertEqual(len({state["machine"] for state in final.values()}), 3,
                         "...and the states are not")


class ShardingTests(unittest.TestCase):
    def test_balance_and_locality_pull_against_each_other(self) -> None:
        expected = {
            # strategy: (passes the balance check, shards a normal tenant scan touches)
            "range": (False, 1),
            "composite": (False, 1),
            "hash": (True, 6),
            "salted": (True, 1),
        }
        for strategy, (balanced, cold_scan) in expected.items():
            with self.subTest(strategy=strategy):
                payload = run("shard-strategies", strategy=strategy)
                metrics = payload["summary"]["metrics"]
                self.assertEqual("分片负载均衡 (load balance)" not in violations(payload), balanced)
                self.assertEqual(metrics["普通租户扫描触及分片"], cold_scan)

    def test_no_strategy_wins_on_both_axes(self) -> None:
        for strategy in ("range", "hash", "composite", "salted"):
            payload = run("shard-strategies", strategy=strategy)
            balanced = "分片负载均衡 (load balance)" not in violations(payload)
            local = payload["summary"]["metrics"]["热点租户扫描触及分片"] == 1
            self.assertFalse(balanced and local,
                             f"{strategy} claims both balance and hot-key locality")

    def test_routing_is_stable_across_processes(self) -> None:
        """Python's hash() is randomised per process; the lab must not depend on it."""
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from cluster_lab.nodes.routing import shard_for;"
            "print([shard_for(f'tenant-{i}/item-{i}', 'hash', 8) for i in range(12)])"
            % str(LAB_ROOT)
        )
        outputs = []
        for hash_seed in ("1", "2"):
            result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                    text=True, env={**os.environ, "PYTHONHASHSEED": hash_seed})
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout.strip())
        self.assertEqual(outputs[0], outputs[1])


class RebalanceTests(unittest.TestCase):
    def test_a_stale_route_is_recovered_by_redirecting(self) -> None:
        for seed in SEEDS:
            with self.subTest(seed=seed):
                payload = run("rebalance-routing", stale_route="reject", seed=seed)
                self.assertEqual(violations(payload), set())
        payload = run("rebalance-routing", stale_route="reject")
        self.assertGreaterEqual(payload["summary"]["metrics"]["重定向次数 (redirects)"], 1)

    def test_serving_a_stale_route_writes_to_the_wrong_shard(self) -> None:
        payload = run("rebalance-routing", stale_route="accept")
        self.assertIn("请求落到正确分片 (routed to the owner)", violations(payload))
        self.assertIn("线性一致 (linearizable)", violations(payload))


class AriesTests(unittest.TestCase):
    def test_recovery_is_correct_when_both_ordering_rules_hold(self) -> None:
        for step in range(1, 10):
            with self.subTest(crash_at_step=step):
                self.assertEqual(violations(run("aries-recovery", crash_at_step=step)), set())

    def test_a_page_flushed_ahead_of_its_log_leaves_an_uncommitted_change(self) -> None:
        payload = run("aries-recovery", wal_policy="no_wal", crash_at_step=6)
        self.assertIn("恢复结果正确 (winners in, losers out)", violations(payload))
        self.assertEqual(payload["facts"]["recovered"]["c"], 30,
                         "the loser's update should have survived on disk")
        self.assertEqual(payload["facts"]["expected"]["c"], 0)

    def test_acking_a_commit_before_it_is_durable_loses_it(self) -> None:
        lost = run("aries-recovery", commit_policy="ack_then_fsync", crash_at_step=8)
        self.assertIn("已确认提交不丢失 (durable commits)", violations(lost))
        safe = run("aries-recovery", commit_policy="fsync_then_ack", crash_at_step=8)
        self.assertEqual(violations(safe), set())
        self.assertEqual(safe["facts"]["recovered"]["d"], 40)

    def test_redo_skips_updates_the_page_already_reflects(self) -> None:
        payload = run("aries-recovery", crash_at_step=6)
        applied, skipped = payload["summary"]["metrics"]["Redo 应用 / 跳过"].split(" / ")
        self.assertGreater(int(skipped), 0, "pageLSN gating never skipped anything")
        self.assertGreater(int(applied), 0)

    def test_clrs_stop_a_second_crash_from_repeating_undo_work(self) -> None:
        with_clr = run("aries-recovery", crash_at_step=6, crash_during_recovery="on",
                       write_clr="on")
        without = run("aries-recovery", crash_at_step=6, crash_during_recovery="on",
                      write_clr="off")
        self.assertLessEqual(with_clr["summary"]["metrics"]["撤销步数 (undo steps)"],
                             without["summary"]["metrics"]["撤销步数 (undo steps)"])
        # Repeating undo is wasted work, not a safety problem: both still recover.
        self.assertEqual(violations(with_clr), set())
        self.assertEqual(violations(without), set())


class JoinStrategyTests(unittest.TestCase):
    def test_a_local_join_on_uncolocated_data_silently_loses_rows(self) -> None:
        payload = run("join-strategies", strategy="local", colocated="no")
        self.assertIn("Join 结果完整 (no lost rows)", violations(payload))
        produced, truth = payload["summary"]["metrics"]["结果行数 / 正确行数"].split(" / ")
        self.assertLess(int(produced), int(truth))

    def test_broadcast_and_repartition_always_return_every_row(self) -> None:
        for strategy in ("broadcast", "shuffle"):
            for colocated in ("yes", "no"):
                with self.subTest(strategy=strategy, colocated=colocated):
                    payload = run("join-strategies", strategy=strategy, colocated=colocated,
                                  memory_mb=8.0)
                    self.assertEqual(violations(payload), set())

    def test_a_colocated_local_join_moves_nothing(self) -> None:
        payload = run("join-strategies", strategy="local", colocated="yes")
        self.assertEqual(violations(payload), set())
        self.assertEqual(payload["summary"]["metrics"]["网络字节 (network)"], "0.00 MB")

    def test_the_cheaper_strategy_crosses_over_as_the_small_side_grows(self) -> None:
        cheap = run("join-strategies", right_rows=60)["summary"]["metrics"]["更便宜的一种"]
        dear = run("join-strategies", right_rows=1200)["summary"]["metrics"]["更便宜的一种"]
        self.assertEqual(cheap, "Broadcast")
        self.assertEqual(dear, "Repartition")

    def test_broadcasting_more_than_fits_is_reported(self) -> None:
        payload = run("join-strategies", strategy="broadcast", right_rows=1200, memory_mb=0.2)
        self.assertIn("广播端放得进内存 (broadcast fits)", violations(payload))


class PushdownTests(unittest.TestCase):
    def test_each_level_of_pushdown_moves_strictly_less(self) -> None:
        moved = []
        for level in ("none", "predicate", "predicate_projection", "full"):
            payload = run("pushdown-aggregation", pushdown=level)
            self.assertEqual(violations(payload), set())
            moved.append(payload["summary"]["metrics"]["跨网络字节 (bytes)"])
        self.assertEqual(moved, sorted(moved, reverse=True))
        self.assertLess(moved[-1], moved[0] / 100)

    def test_averaging_averages_is_wrong_once_it_is_pushed_down(self) -> None:
        payload = run("pushdown-aggregation", pushdown="full", aggregate="avg_naive")
        self.assertIn("聚合结果正确 (mergeable aggregate)", violations(payload))

    def test_the_same_average_is_correct_when_decomposed(self) -> None:
        payload = run("pushdown-aggregation", pushdown="full", aggregate="avg_decomposed")
        self.assertEqual(violations(payload), set())

    def test_a_non_mergeable_aggregate_is_only_wrong_when_pushed_down(self) -> None:
        payload = run("pushdown-aggregation", pushdown="predicate", aggregate="avg_naive")
        self.assertEqual(violations(payload), set())


class CoordinationTests(unittest.TestCase):
    def test_without_a_commit_protocol_value_leaks(self) -> None:
        payload = run("order-vs-coordinate", mode="none")
        self.assertIn("总额守恒 (value conserved)", violations(payload))

    def test_both_correct_designs_conserve_value_through_a_crash(self) -> None:
        for mode in ("coordinate", "preorder"):
            for seed in SEEDS:
                with self.subTest(mode=mode, seed=seed):
                    self.assertEqual(violations(run("order-vs-coordinate", mode=mode,
                                                    seed=seed)), set())

    def test_preordering_removes_cross_shard_messages_from_execution(self) -> None:
        two_phase = run("order-vs-coordinate", mode="coordinate")["summary"]["metrics"]
        calvin = run("order-vs-coordinate", mode="preorder")["summary"]["metrics"]
        self.assertGreater(two_phase["执行期跨分片消息"], 0)
        self.assertEqual(calvin["执行期跨分片消息"], 0)
        self.assertLess(calvin["协调往返 (round trips)"], two_phase["协调往返 (round trips)"])

    def test_determinism_does_not_remove_the_need_to_agree_on_the_input(self) -> None:
        calvin = run("order-vs-coordinate", mode="preorder")["summary"]["metrics"]
        self.assertGreater(calvin["协调往返 (round trips)"], 0)

    def test_an_unknown_read_write_set_costs_a_reconnaissance_round(self) -> None:
        known = run("order-vs-coordinate", mode="preorder", rw_set="known")
        dependent = run("order-vs-coordinate", mode="preorder", rw_set="dependent")
        self.assertEqual(known["summary"]["metrics"]["侦察往返 (reconnaissance)"], 0)
        self.assertGreater(dependent["summary"]["metrics"]["侦察往返 (reconnaissance)"], 0)


class VerificationLoopTests(unittest.TestCase):
    def test_the_search_finds_the_stale_read_by_itself(self) -> None:
        payload = run("jepsen-loop", read="leader_only", nemesis="partition")
        self.assertIn("线性一致 (linearizable)", violations(payload))
        self.assertNotEqual(payload["summary"]["metrics"]["反例种子"], "未找到")

    def test_correct_read_protocols_survive_the_whole_search(self) -> None:
        for read in ("readindex", "follower_read"):
            for nemesis in ("partition", "crash"):
                with self.subTest(read=read, nemesis=nemesis):
                    payload = run("jepsen-loop", read=read, nemesis=nemesis, seeds=20)
                    self.assertEqual(violations(payload), set())
                    self.assertEqual(payload["summary"]["metrics"]["反例种子"], "未找到")

    def test_a_stale_read_needs_a_partition_to_appear(self) -> None:
        """Without an isolated leader there is nothing stale to read from."""
        payload = run("jepsen-loop", read="leader_only", nemesis="none", seeds=20)
        self.assertEqual(violations(payload), set())

    def test_the_reported_counterexample_is_small(self) -> None:
        payload = run("jepsen-loop", read="leader_only", nemesis="partition")
        self.assertLessEqual(payload["summary"]["metrics"]["最小反例操作数"], 4)


class ArchitectureTests(unittest.TestCase):
    ARCHITECTURES = ("spanner", "cockroachdb", "calvin", "foundationdb", "tidb")

    def test_every_path_is_walked_as_declared(self) -> None:
        for architecture in self.ARCHITECTURES:
            with self.subTest(architecture=architecture):
                payload = run("architecture-lenses", architecture=architecture)
                self.assertEqual(violations(payload), set())

    def test_each_architecture_answers_all_four_questions(self) -> None:
        for architecture in self.ARCHITECTURES:
            metrics = run("architecture-lenses", architecture=architecture)["summary"]["metrics"]
            for question in ("顺序 / 时间戳由谁分配", "冲突何时被发现",
                             "成功之前还要等什么", "成功之后的异步工作"):
                self.assertTrue(metrics[question].strip(),
                                f"{architecture} left {question} blank")

    def test_only_some_architectures_do_work_after_the_reply(self) -> None:
        after_reply = set()
        for architecture in self.ARCHITECTURES:
            payload = run("architecture-lenses", architecture=architecture)
            reply_at = payload["facts"]["reply_at"]
            if any(event["type"] == "arch.phase" and event["t"] > reply_at
                   for event in payload["events"]):
                after_reply.add(architecture)
        self.assertEqual(after_reply, {"cockroachdb", "tidb"})


class BoundedCheckerTests(unittest.TestCase):
    """A checker that gave up must never be indistinguishable from one that passed."""

    def test_a_history_within_budget_is_actually_checked(self) -> None:
        check = run("jepsen-loop", ops=9, nemesis="none",
                    read="readindex")["summary"]["checks"][0]
        self.assertEqual(check["status"], "pass")

    def test_a_history_over_budget_is_reported_as_not_checked(self) -> None:
        payload = run("jepsen-loop", ops=20, nemesis="none", read="readindex")
        check = payload["summary"]["checks"][0]
        self.assertEqual(check["status"], "inconclusive")
        self.assertIn("未检查", check["detail"])
        self.assertEqual(payload["summary"]["violations"], [])
        self.assertIn(check["name"], payload["summary"]["inconclusive"])

    def test_every_requested_operation_actually_runs(self) -> None:
        """Operations must fit before the horizon whatever the instructor asks for."""
        for ops in (4, 9, 16, 24):
            with self.subTest(ops=ops):
                payload = run("jepsen-loop", ops=ops, nemesis="none", read="readindex")
                self.assertEqual(payload["summary"]["metrics"]["历史操作数"], ops)


class ParameterValidationTests(unittest.TestCase):
    """Cross-field rules: a range check on each field alone cannot express these."""

    INVALID = [
        {"delay_min": 200, "delay_max": 18},
        {"timeout_min": 2000, "timeout_max": 300},
        {"timeout_min": 20, "timeout_max": 20},
    ]

    def test_invalid_pairs_are_rejected_with_an_actionable_message(self) -> None:
        for options in self.INVALID:
            with self.subTest(**options):
                with self.assertRaises(ValueError) as caught:
                    run("raft-election", **options)
                message = str(caught.exception)
                for name in options:
                    label = next(param.label for param in registry.get("raft-election").params.params
                                 if param.name == name)
                    self.assertIn(label, message)

    def test_the_http_api_answers_400_rather_than_failing_mid_run(self) -> None:
        httpd = server.serve(port=0)
        Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            request = urllib.request.Request(
                f"{base}/api/run",
                data=json.dumps({"scenario": "raft-election",
                                 "params": {"delay_min": 200, "delay_max": 18}}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=20)
            self.assertEqual(caught.exception.code, 400)
            with caught.exception as response:
                self.assertIn("error", json.loads(response.read()))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_every_scenario_that_exposes_a_pair_declares_the_rule(self) -> None:
        for scenario in registry.all_scenarios():
            names = {param.name for param in scenario.params.params}
            rules = {(rule.lower, rule.upper) for rule in scenario.params.rules}
            if {"delay_min", "delay_max"} <= names:
                self.assertIn(("delay_min", "delay_max"), rules, scenario.id)
            if {"timeout_min", "timeout_max"} <= names:
                self.assertIn(("timeout_min", "timeout_max"), rules, scenario.id)

    def test_defaults_are_always_valid(self) -> None:
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                scenario.params.validate(scenario.params.defaults())

    def test_a_heartbeat_close_to_the_election_timeout_is_flagged_not_blocked(self) -> None:
        payload = run("raft-election", heartbeat=200, timeout_min=250, timeout_max=400)
        self.assertTrue(payload["summary"]["advisories"])
        self.assertEqual(run("raft-election")["summary"]["advisories"], [])


class InstructorScriptFiguresTests(unittest.TestCase):
    """Every exact number printed in README.md's classroom scripts.

    Course material that quotes figures has to be regression-tested, otherwise the
    lecturer reads one number aloud while the projector shows another.
    """

    @staticmethod
    def metrics(scenario: str, **options):
        return run(scenario, **options)["summary"]["metrics"]

    def test_sharding_tradeoff_table(self) -> None:
        expected = {
            "range": ("3.83", 1, 1),
            "hash": ("1.33", 6, 6),
            "composite": ("4.00", 1, 1),
            "salted": ("1.93", 6, 1),
        }
        for strategy, (ratio, hot, cold) in expected.items():
            with self.subTest(strategy=strategy):
                metrics = self.metrics("shard-strategies", strategy=strategy)
                self.assertEqual(metrics["最热/平均"], ratio)
                self.assertEqual(metrics["热点租户扫描触及分片"], hot)
                self.assertEqual(metrics["普通租户扫描触及分片"], cold)

    def test_coordination_comparison_table(self) -> None:
        expected = {
            "none": (12, 0, "4000 / 3942"),
            "coordinate": (24, 113, "4000 / 4000"),
            "preorder": (6, 0, "4000 / 4000"),
        }
        for mode, (round_trips, cross_shard, total) in expected.items():
            with self.subTest(mode=mode):
                metrics = self.metrics("order-vs-coordinate", mode=mode)
                self.assertEqual(metrics["协调往返 (round trips)"], round_trips)
                self.assertEqual(metrics["执行期跨分片消息"], cross_shard)
                self.assertEqual(metrics["总额 (期望 / 实际)"], total)

    def test_lost_rows_in_the_uncolocated_local_join(self) -> None:
        metrics = self.metrics("join-strategies", strategy="local", colocated="no")
        self.assertEqual(metrics["结果行数 / 正确行数"], "1727 / 6918")

    def test_pushdown_ladder_bytes(self) -> None:
        ladder = [self.metrics("pushdown-aggregation", pushdown=level)["跨网络字节 (bytes)"]
                  for level in ("none", "predicate", "predicate_projection", "full")]
        self.assertEqual(ladder, [307200, 69120, 17280, 128])

    def test_two_phase_commit_blocking_window(self) -> None:
        single = self.metrics("two-phase-commit", crash_point="after_decision",
                              coordinator_log="single")
        replicated = self.metrics("two-phase-commit", crash_point="after_decision",
                                  coordinator_log="replicated")
        self.assertEqual(single["T1 阻塞时长 (ticks)"], "1892")
        self.assertEqual(replicated["T1 阻塞时长 (ticks)"], "1103")

    def test_divergent_replica_tokens(self) -> None:
        metrics = self.metrics("nondeterministic-apply", mode="at_apply")
        self.assertEqual(metrics["A 的状态"].split()[0], "t1=A@280")
        self.assertEqual(metrics["B 的状态"].split()[0], "t1=B@320")
        self.assertEqual(metrics["C 的状态"].split()[0], "t1=C@317")

    def test_the_clock_counterexample_pair(self) -> None:
        checks = run("four-clocks", clock="physical", skew="on")["summary"]["checks"]
        self.assertIn("b1(160)", checks[0]["detail"])
        self.assertIn("c2(90)", checks[0]["detail"])

    def test_the_search_reports_a_two_operation_counterexample(self) -> None:
        metrics = self.metrics("jepsen-loop", read="leader_only", nemesis="partition")
        self.assertEqual(metrics["最小反例操作数"], 2)

    def test_the_wal_violation_leaves_the_loser_value_on_disk(self) -> None:
        metrics = self.metrics("aries-recovery", wal_policy="no_wal")
        self.assertEqual(metrics["崩溃时日志持久化到"], "LSN 4")
        self.assertIn("c=30", metrics["恢复结果"])


class DocumentationTests(unittest.TestCase):
    """Course material is part of the product: keep it checkable."""

    README = (LAB_ROOT / "README.md").read_text(encoding="utf-8")

    def test_every_path_in_the_readme_directory_listing_exists(self) -> None:
        block = self.README.split("## 目录", 1)[1].split("```text", 1)[1].split("```", 1)[0]
        checked = 0
        for line in block.splitlines():
            for entry in line.strip().split():
                if not entry.endswith((".py", ".js", ".css", ".html", ".md")):
                    continue
                self.assertTrue(list(LAB_ROOT.rglob(entry)),
                                f"README lists {entry!r}, which does not exist")
                checked += 1
        self.assertGreater(checked, 20, "the listing check found suspiciously little")

    def test_every_registered_demo_appears_in_the_instructor_script(self) -> None:
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                self.assertIn(f"`{scenario.id}`", self.README)


class ApiTests(unittest.TestCase):
    def test_sweep_reports_every_seed(self) -> None:
        report = server.sweep("ack-meaning", {"ack": "majority", "retry": "same_id"}, 2026, 5)
        self.assertEqual(report["checked"], 5)
        self.assertEqual(report["failing"], 0)

    def test_sweep_finds_the_broken_configuration_on_every_seed(self) -> None:
        report = server.sweep("ack-meaning", {"ack": "local"}, 2026, 5)
        self.assertEqual(report["failing"], 5)

    def test_compare_requires_a_declared_axis(self) -> None:
        result = server.compare("ack-meaning", {}, "ack", ["local", "majority"])
        self.assertEqual(len(result["sides"]), 2)
        with self.assertRaises(ValueError):
            server.compare("ack-meaning", {}, "client_timeout", [100, 200])

    def test_http_endpoints_answer(self) -> None:
        httpd = server.serve(port=0)
        Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(f"{base}/api/scenarios", timeout=10) as response:
                listing = json.loads(response.read())
            self.assertTrue(listing["scenarios"])

            request = urllib.request.Request(
                f"{base}/api/run",
                data=json.dumps({"scenario": "raft-election", "params": {"seed": 7}}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["seed"], 7)
            self.assertTrue(payload["events"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_index_page_is_served(self) -> None:
        self.assertTrue((server.WEB_ROOT / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
