import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import debate_lib  # noqa: E402


DOC = """# Debate: sample

**Status:** OPEN

## Proposal

STATUS: OPEN

[A1-R1] initial proposal
~~old claim [A1-R1]~~ [A2-R1] counter

## Parking Lot

- [A1-R1] someone wrote STATUS: CONVERGED in prose here

## Dispute Log

| Round | Agent | Section | What Changed | Why | Status |
|-------|-------|---------|--------------|-----|--------|
| | | | | | |
| 1 | A2 | Proposal | countered claim | evidence | OPEN |
"""


class SectionParsingTest(unittest.TestCase):
    def test_proposal_not_converged_despite_mention_elsewhere(self):
        self.assertFalse(
            debate_lib.section_has_status_line(DOC, "## Proposal", "STATUS: CONVERGED")
        )

    def test_proposal_converged_detected_in_section_only(self):
        converged = DOC.replace("STATUS: OPEN\n\n[A1-R1]", "STATUS: CONVERGED\n\n[A1-R1]")
        self.assertTrue(
            debate_lib.section_has_status_line(converged, "## Proposal", "STATUS: CONVERGED")
        )

    def test_plan_converged(self):
        doc = "## Plan\n\nPLAN_STATUS: CONVERGED\n\n## Parking Lot\n"
        self.assertTrue(
            debate_lib.section_has_status_line(doc, "## Plan", "PLAN_STATUS: CONVERGED")
        )
        doc_open = doc.replace("CONVERGED", "OPEN")
        self.assertFalse(
            debate_lib.section_has_status_line(doc_open, "## Plan", "PLAN_STATUS: CONVERGED")
        )


class DisputeLogTest(unittest.TestCase):
    def test_open_dispute_detected(self):
        self.assertTrue(debate_lib.has_open_disputes(DOC))

    def test_open_dispute_without_trailing_pipe_detected(self):
        doc = DOC.replace(
            "| 1 | A2 | Proposal | countered claim | evidence | OPEN |",
            "| 1 | A2 | Proposal | countered claim | evidence | OPEN",
        )
        self.assertTrue(debate_lib.has_open_disputes(doc))

    def test_closed_and_parked_do_not_block(self):
        doc = DOC.replace("| OPEN |", "| CLOSED |")
        self.assertFalse(debate_lib.has_open_disputes(doc))
        doc = DOC.replace("| OPEN |", "| PARKED |")
        self.assertFalse(debate_lib.has_open_disputes(doc))

    def test_template_empty_row_ignored(self):
        doc = DOC.replace("| 1 | A2 | Proposal | countered claim | evidence | OPEN |\n", "")
        self.assertFalse(debate_lib.has_open_disputes(doc))


class TagPreservationTest(unittest.TestCase):
    def check(self, old, new):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_path = Path(tmpdir) / "old.md"
            new_path = Path(tmpdir) / "new.md"
            old_path.write_text(old)
            new_path.write_text(new)
            return subprocess.run(
                [sys.executable, str(ROOT / "debate_lib.py"), "verify-preservation", str(old_path), str(new_path)],
                capture_output=True,
                text=True,
            )

    def test_collects_tags_with_counts(self):
        self.assertEqual(
            debate_lib.collect_turn_tags(DOC), {"[A1-R1]": 3, "[A2-R1]": 1}
        )

    def test_dropping_one_duplicate_occurrence_fails(self):
        new = DOC.replace("[A1-R1] initial proposal", "rewritten proposal")
        result = self.check(DOC, new)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[A1-R1]", result.stdout)

    def test_normal_addition_passes(self):
        new = DOC + "\n[A1-R2] follow-up edit\n"
        result = self.check(DOC, new)
        self.assertEqual(result.returncode, 0)

    def test_dropped_tag_fails(self):
        new = DOC.replace("~~old claim [A1-R1]~~ [A2-R1] counter", "rewritten history")
        result = self.check(DOC, new)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[A2-R1]", result.stdout)

    def test_shrunk_dispute_log_fails(self):
        new = DOC.replace("| 1 | A2 | Proposal | countered claim | evidence | OPEN |\n", "")
        new += "\n[A2-R1] keep tag present\n"
        result = self.check(DOC, new)
        self.assertEqual(result.returncode, 1)
        self.assertIn("dispute log shrank", result.stdout)

    def test_rewriting_an_earlier_tagged_line_fails(self):
        old = "# Debate: X\n[A1-R1] original claim\n## Dispute Log\n"
        new = "# Debate: X\n[A1-R1] opposite claim\n## Dispute Log\n"

        result = self.check(old, new)

        self.assertEqual(result.returncode, 1)
        self.assertIn("rewrote earlier tagged lines", result.stdout)
        self.assertIn("missing original line: [A1-R1] original claim", result.stdout)

    def test_strikethrough_markup_change_preserves_tagged_line(self):
        old = "# Debate: X\n[A1-R1] original claim\n## Dispute Log\n"
        new = "# Debate: X\n~~[A1-R1] original claim~~\n## Dispute Log\n"

        result = self.check(old, new)

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_dispute_status_change_preserves_tagged_row(self):
        old = (
            "# Debate: X\n## Dispute Log\n"
            "| Round | Agent | Section | What Changed | Why | Status |\n"
            "| R1 | A1 | Proposal | [A1-R1] claim | evidence | OPEN |\n"
        )
        new = old.replace("| OPEN |", "| CLOSED |")

        result = self.check(old, new)

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_detects_substantive_proposal_change_but_ignores_status(self):
        old = "# Debate: X\n## Proposal\nSTATUS: OPEN\n[A1-R1] claim\n## Dispute Log\n"
        status_only = old.replace("STATUS: OPEN", "STATUS: CONVERGED")
        changed = status_only.replace("[A1-R1] claim", "[A1-R1] claim\n[A2-R1] new condition")

        self.assertFalse(debate_lib.has_substantive_change(old, status_only))
        self.assertTrue(debate_lib.has_substantive_change(old, changed))
        self.assertTrue(
            debate_lib.has_substantive_change(old, old.replace("[A1-R1] claim", "~~[A1-R1] claim~~"))
        )

    def test_no_change_note_is_not_substantive(self):
        old = "# Debate: X\n## Proposal\nSTATUS: CONVERGED\n- [A1-R1] claim\n## Dispute Log\n"
        noted = old.replace("- [A1-R1] claim", "- [A1-R1] claim\n  - [A2-R5] Reviewed; no change to P1.")
        no_change = old.replace("- [A1-R1] claim", "- [A1-R1] claim\n[A2-R5] No change.")

        self.assertFalse(debate_lib.has_substantive_change(old, noted))
        self.assertFalse(debate_lib.has_substantive_change(old, no_change))


class RestoreAndNoteTest(unittest.TestCase):
    LONG = "[A1-R1] the parser says so when the input file is missing from the disk entirely"

    def run_lib(self, command, old, new_or_text):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_path = Path(tmpdir) / "old.md"
            new_path = Path(tmpdir) / "new.md"
            old_path.write_text(old, encoding="utf-8")
            new_path.write_text(new_or_text, encoding="utf-8")
            args = [str(old_path), str(new_path)] if command != "add-note" else [str(old_path), new_or_text]
            result = subprocess.run(
                [sys.executable, str(ROOT / "debate_lib.py"), command, *args],
                capture_output=True, text=True, encoding="utf-8",
            )
            return result, new_path.read_text(encoding="utf-8"), old_path.read_text(encoding="utf-8")

    def test_one_word_slip_is_restored(self):
        old = f"# Debate: X\n{self.LONG}\n## Dispute Log\n"
        new = old.replace("says so", "says no") + "[A1-R2] more\n"
        result, text, _ = self.run_lib("restore-rewritten", old, new)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(self.LONG, text)
        self.assertIn("[A1-R2] more", text)
        self.assertIn("restored:", result.stdout)

    def test_new_line_quoting_an_old_tag_is_not_mistaken_for_a_rewrite(self):
        old = f"# Debate: X\n{self.LONG}\n## Dispute Log\n"
        new = old.replace(self.LONG, "") + f"[A2-R2] {self.LONG}\n"
        result, text, _ = self.run_lib("restore-rewritten", old, new)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(text, new)

    def test_deleted_line_is_not_restored(self):
        old = f"# Debate: X\n{self.LONG}\n## Dispute Log\n"
        new = "# Debate: X\n[A1-R2] other\n## Dispute Log\n"
        result, _, _ = self.run_lib("restore-rewritten", old, new)
        self.assertEqual(result.returncode, 1)

    def test_short_line_is_not_restored(self):
        old = "# Debate: X\n[A1-R1] I agree\n## Dispute Log\n"
        result, _, _ = self.run_lib("restore-rewritten", old, old.replace("agree", "disagree"))
        self.assertEqual(result.returncode, 1)

    def test_add_note_goes_under_parking_lot(self):
        old = "# Debate: X\n## Parking Lot\n\n- [A1-R1] item\n\n## Dispute Log\n"
        _, _, text = self.run_lib("add-note", old, "- [A2-R2] note")
        self.assertIn("- [A1-R1] item\n- [A2-R2] note\n\n## Dispute Log", text)

    def test_add_note_without_parking_lot_inserts_before_dispute_log(self):
        old = "# Debate: X\n## Dispute Log\n"
        _, _, text = self.run_lib("add-note", old, "- [A2-R2] note")
        self.assertLess(text.index("## Parking Lot"), text.index("## Dispute Log"))
        self.assertIn("- [A2-R2] note", text)


class EventsConvergedTest(unittest.TestCase):
    @staticmethod
    def turns(*specs):
        return [
            {"type": "turn", "agent_index": idx, "status": status, **({"ended_converged": True} if flag else {})}
            for idx, status, flag in specs
        ]

    def test_everyone_took_a_turn_without_barrier(self):
        self.assertTrue(debate_lib.events_converged(self.turns((1, "completed", 0), (2, "completed", 0)), 2))
        self.assertFalse(debate_lib.events_converged(self.turns((1, "completed", 0)), 2))

    def test_two_agent_round_one_converges_when_the_reviewer_ends_converged(self):
        events = self.turns((1, "completed_substantive", 0), (2, "completed", 1))
        self.assertTrue(debate_lib.events_converged(events, 2))

    def test_reviewers_who_left_status_open_do_not_count_when_the_author_flips_it(self):
        events = self.turns(
            (1, "completed_substantive", 0), (2, "completed", 0), (3, "completed", 0), (1, "completed", 1)
        )
        self.assertFalse(debate_lib.events_converged(events, 3))
        events += self.turns((2, "confirmed", 0), (3, "confirmed", 0))
        self.assertTrue(debate_lib.events_converged(events, 3))

    def test_agreeing_agents_after_a_substantive_change(self):
        events = self.turns((1, "completed_substantive", 0), (2, "completed", 0))
        self.assertEqual(debate_lib.agreeing_agents(events), ({1}, True))
        self.assertEqual(debate_lib.agreeing_agents(self.turns((1, "completed", 1))), ({1}, False))

    def test_authors_substantive_converged_turn_alone_is_not_converged(self):
        events = self.turns((1, "completed", 1), (2, "completed_substantive", 1))
        self.assertFalse(debate_lib.events_converged(events, 2))

    def test_preexisting_after_barrier_does_not_count(self):
        events = self.turns((2, "completed_substantive", 0), (1, "preexisting", 0))
        self.assertFalse(debate_lib.events_converged(events, 2))
        self.assertTrue(debate_lib.events_converged(self.turns((1, "preexisting", 0), (2, "completed", 0)), 2))


class RenderTemplateTest(unittest.TestCase):
    def render(self, env_overrides):
        env = os.environ.copy()
        env.update(
            {
                "DL_TOPIC": "plain topic",
                "DL_DATE": "2026-07-02",
                "DL_MAX_ROUNDS": "3",
                "DL_CONSTRAINTS": "",
                "DL_AGENT_COUNT": "2",
                "DL_AGENT1": "Opus",
                "DL_AGENT2": "Codex",
                "DL_AGENT3": "",
                "DL_AGENT4": "",
                "DL_FILES": "",
            }
        )
        env.update(env_overrides)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "debate.md"
            result = subprocess.run(
                [sys.executable, str(ROOT / "debate_lib.py"), "render", str(ROOT / "TEMPLATE.md"), str(out)],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return out.read_text()

    def test_topic_with_sed_metacharacters_is_verbatim(self):
        text = self.render({"DL_TOPIC": "use X | Y & Z\\ or not?"})
        self.assertIn("# Debate: use X | Y & Z\\ or not?", text)

    def test_two_agents_drop_agent_3_and_4_lines(self):
        text = self.render({})
        self.assertNotIn("Agent 3", text)
        self.assertNotIn("Agent 4", text)
        self.assertNotIn("{AGENT_3_NAME}", text)

    def test_four_agents_keep_all_names(self):
        text = self.render(
            {"DL_AGENT_COUNT": "4", "DL_AGENT3": "Gemini", "DL_AGENT4": "Copilot"}
        )
        self.assertIn("**Agent 3:** Gemini", text)
        self.assertIn("**Agent 4:** Copilot", text)

    def test_file_list_renders_one_bullet_per_line(self):
        text = self.render({"DL_FILES": "src/a.py\nsrc/b.py\n"})
        self.assertIn("- `src/a.py`\n- `src/b.py`", text)

    def test_no_files_renders_default_text(self):
        text = self.render({})
        self.assertIn("None specified — agents should explore as needed.", text)

    def test_constraints_default(self):
        text = self.render({})
        self.assertIn("None specified.", text)


if __name__ == "__main__":
    unittest.main()
