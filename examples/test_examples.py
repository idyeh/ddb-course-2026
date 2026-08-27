import unittest

from deterministic_network import scenario
from history_checker import Operation, is_linearizable
from lamport_clock import Process
from raft_rules import can_advance_commit, candidate_is_up_to_date, election_timeouts
from recovery import demonstration
from sharding import modulo_bucket, rendezvous_node
from write_skew import under_snapshot_isolation, with_shared_guard


class ExampleTests(unittest.TestCase):
    def test_network_seed_replays_the_same_trace(self) -> None:
        self.assertEqual(scenario(2026), scenario(2026))
        self.assertNotEqual(scenario(2026), scenario(2027))

    def test_lamport_receive_advances_beyond_both_clocks(self) -> None:
        receiver = Process("receiver", clock=7)
        self.assertEqual(receiver.receive(11), 12)

    def test_raft_candidate_compares_term_before_index(self) -> None:
        self.assertTrue(candidate_is_up_to_date(4, 2, 3, 100))
        self.assertFalse(candidate_is_up_to_date(3, 100, 4, 2))

    def test_raft_only_counts_current_term_entry_for_commit_advance(self) -> None:
        matches = [4, 4, 4, 2, 1]
        self.assertTrue(can_advance_commit(matches, [1, 2, 2, 3], 4, 3))
        self.assertFalse(can_advance_commit(matches, [1, 2, 2, 2], 4, 3))

    def test_election_timeout_seed_is_repeatable(self) -> None:
        self.assertEqual(election_timeouts(2026, 5), election_timeouts(2026, 5))

    def test_sharding_is_stable(self) -> None:
        self.assertEqual(modulo_bucket("订单-42", 16), modulo_bucket("订单-42", 16))
        nodes = ["n1", "n2", "n3"]
        self.assertEqual(rendezvous_node("k", nodes), rendezvous_node("k", nodes))

    def test_write_skew_and_guard(self) -> None:
        self.assertEqual(under_snapshot_isolation().on_call_count, 0)
        guarded, outcomes = with_shared_guard()
        self.assertEqual(guarded.on_call_count, 1)
        self.assertEqual(outcomes, (True, False))

    def test_recovery_redoes_winner_and_undoes_loser(self) -> None:
        self.assertEqual(demonstration(), {"x": 10, "y": 0})

    def test_register_history_checker(self) -> None:
        valid = [
            Operation("w", "write", 1, None, 0, 2),
            Operation("r", "read", None, 1, 1, 3),
        ]
        invalid = [
            Operation("w", "write", 1, None, 0, 1),
            Operation("r", "read", None, 0, 2, 3),
        ]
        self.assertTrue(is_linearizable(valid))
        self.assertFalse(is_linearizable(invalid))


if __name__ == "__main__":
    unittest.main()

