import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GuardrailsContractTest(unittest.TestCase):
    def test_preservation_and_round_one_closure_rules_are_source_controlled(self):
        text = (ROOT / "agent-guardrails.md").read_text()

        self.assertIn("## Preservation invariant", text)
        self.assertIn("Never delete an existing line containing an earlier turn tag", text)
        self.assertIn("may converge at the end of any completed round", text)
        self.assertIn("introduce or modify substantive proposal content", text)
        self.assertNotIn("After Round 2", text)


if __name__ == "__main__":
    unittest.main()
