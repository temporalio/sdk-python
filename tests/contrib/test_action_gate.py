import importlib.util
import os
import sys
import unittest

# Load module directly
file_path = os.path.join(
    os.path.dirname(__file__),
    "../../temporalio/contrib/action_gate.py",
)
spec = importlib.util.spec_from_file_location("temporal_action_gate", file_path)
action_gate_mod = importlib.util.module_from_spec(spec)
sys.modules["temporal_action_gate"] = action_gate_mod
spec.loader.exec_module(action_gate_mod)

ActionGateActivityInterceptor = action_gate_mod.ActionGateActivityInterceptor
TemporalActionLedger = action_gate_mod.TemporalActionLedger
GENESIS_HASH = action_gate_mod.GENESIS_HASH


class TestActionGateActivityInterceptor(unittest.TestCase):
    def setUp(self):
        self.interceptor = ActionGateActivityInterceptor(
            never_equate_intent_to_approval=True,
            enforce_action_boundary=True,
        )

    def test_intercept_non_destructive_activity_allowed(self):
        res = self.interceptor.intercept_activity(
            activity_name="fetch_customer_record",
            args=["cust_123"],
            is_destructive=False,
        )
        self.assertTrue(res["allowed"])
        self.assertIn("hash", res)
        entries = self.interceptor.ledger.get_ledger_entries()
        self.assertEqual(len(entries), 1)

    def test_intercept_destructive_activity_without_token_fails(self):
        with self.assertRaises(PermissionError):
            self.interceptor.intercept_activity(
                activity_name="charge_credit_card",
                kwargs={"amount_cents": 50000},
                is_destructive=True,
                prove_token=None,
            )

    def test_intercept_destructive_activity_with_valid_token_passes(self):
        res = self.interceptor.intercept_activity(
            activity_name="charge_credit_card",
            kwargs={"amount_cents": 50000},
            is_destructive=True,
            prove_token="prov_live_1234567890abcdef1234567890abcdef",
        )
        self.assertTrue(res["allowed"])
        entries = self.interceptor.ledger.get_ledger_entries()
        self.assertEqual(len(entries), 1)

    def test_hash_chain_integrity(self):
        self.interceptor.intercept_activity("act1", is_destructive=False)
        self.interceptor.intercept_activity("act2", is_destructive=False)
        self.interceptor.intercept_activity("act3", is_destructive=False)

        entries = self.interceptor.ledger.get_ledger_entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["prev_hash"], GENESIS_HASH)
        self.assertEqual(entries[1]["prev_hash"], entries[0]["curr_hash"])
        self.assertEqual(entries[2]["prev_hash"], entries[1]["curr_hash"])
        self.assertTrue(self.interceptor.ledger.verify_integrity())


if __name__ == "__main__":
    unittest.main()
