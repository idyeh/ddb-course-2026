"""Tests for the lab.

The negative tests matter as much as the positive ones: a demonstration that can
never fail does not demonstrate anything. Each deliberately broken configuration
below is required to produce its counterexample.
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.request
from pathlib import Path
from threading import Thread

LAB_ROOT = Path(__file__).resolve().parents[1]
if str(LAB_ROOT) not in sys.path:
    sys.path.insert(0, str(LAB_ROOT))

from cluster_lab import registry, server  # noqa: E402
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
        for scenario in registry.all_scenarios():
            with self.subTest(scenario=scenario.id):
                payload = run(scenario.id)
                sent, ended = set(), []
                for event in payload["events"]:
                    if event["type"] == "msg.send":
                        self.assertNotIn(event["id"], sent)
                        sent.add(event["id"])
                    elif event["type"] in {"msg.deliver", "msg.drop"}:
                        ended.append(event["id"])
                self.assertEqual(len(ended), len(set(ended)))
                self.assertTrue(set(ended) <= sent)

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
