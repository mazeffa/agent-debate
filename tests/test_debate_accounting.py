import json
import tempfile
import unittest
from pathlib import Path

from debate_accounting import record_attempt


class DebateAccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmpdir.name) / "debate.state.json"
        self.state_file.write_text(json.dumps({"events": [], "attempts": []}))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_records_accepted_and_rejected_costs_in_aggregate(self):
        record_attempt(
            self.state_file,
            {
                "attempt_id": "R1-A1-debate-1",
                "status": "accepted",
                "metering_status": "complete",
                "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20},
                "cost_usd": 0.10,
            },
        )
        record_attempt(
            self.state_file,
            {
                "attempt_id": "R1-A2-debate-1",
                "status": "rejected",
                "metering_status": "complete",
                "usage": {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 5},
                "cost_usd": 0.04,
            },
        )

        state = json.loads(self.state_file.read_text())
        self.assertEqual(len(state["attempts"]), 2)
        self.assertEqual(
            state["usage_totals"],
            {"input_tokens": 150, "cached_input_tokens": 50, "output_tokens": 25},
        )
        self.assertAlmostEqual(state["total_cost_usd"], 0.14)
        self.assertTrue(state["metering_complete"])

    def test_updating_attempt_outcome_does_not_double_charge(self):
        attempt = {
            "attempt_id": "R1-A1-debate-1",
            "status": "provider_completed",
            "metering_status": "complete",
            "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20},
            "cost_usd": 0.10,
        }
        record_attempt(self.state_file, attempt)
        record_attempt(self.state_file, {**attempt, "status": "rejected"})

        state = json.loads(self.state_file.read_text())
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(state["attempts"][0]["status"], "rejected")
        self.assertAlmostEqual(state["total_cost_usd"], 0.10)

    def test_missing_required_metering_marks_totals_incomplete(self):
        record_attempt(
            self.state_file,
            {
                "attempt_id": "R1-A1-debate-1",
                "status": "metering_failed",
                "metering_status": "failed",
                "usage": None,
                "cost_usd": None,
            },
        )

        state = json.loads(self.state_file.read_text())
        self.assertFalse(state["metering_complete"])
        self.assertEqual(state["total_cost_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()
