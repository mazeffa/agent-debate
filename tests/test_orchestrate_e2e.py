import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATE = ROOT / "orchestrate.sh"


DUMMY_AGENT = '''#!/usr/bin/env python3
"""Deterministic stand-in for an agent CLI in orchestrator tests.

Reads the prompt (last argument), extracts the embedded debate document,
adds its required turn tag, and marks the proposal converged. Behavior
toggles via DUMMY_MODE for failure-injection tests.
"""
import os
import json
import sys

prompt = sys.argv[-1]


def emit(text):
    if os.environ.get("DUMMY_JSON") == "1":
        sys.stdout.write(json.dumps({
            "type": "result",
            "subtype": "success",
            "result": text,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 40, "output_tokens": 20},
        }))
    else:
        sys.stdout.write(text)


if "CONFIRM-ONLY TURN" in prompt:
    emit(os.environ.get("DUMMY_CONFIRM", "CONFIRM"))
    sys.exit(0)

doc = prompt[prompt.rfind("# Debate:"):]

name_marker = "You are **Dummy"
idx = prompt[prompt.find(name_marker) + len(name_marker)]
round_marker = "This is Round **"
rnd = prompt[prompt.find(round_marker) + len(round_marker)]
tag = "[A" + idx + "-R" + rnd + "]"

mode = os.environ.get("DUMMY_MODE", "converge")

if mode == "drop_history":
    # Simulate a model that rewrites the file and loses an earlier turn tag.
    doc = doc.replace("[A1-R1] dummy edit by agent 1", "rewritten history")

if mode == "final_substantive" and idx == "2":
    doc = doc.replace(
        "\\n---\\n\\n## Plan",
        "\\n[A2-R" + rnd + "] unreviewed proposal condition\\n\\n---\\n\\n## Plan",
        1,
    )

if mode == "slip" and idx == "2":
    # A one-character transcription slip in an earlier agent's line.
    doc = doc.replace("explanation", "explanatiom")

if mode == "open_then_agree" and idx == "1":
    # A1 proposes substantively and leaves the status OPEN; A2 reviews and sets CONVERGED.
    doc = doc.replace(
        "\\n---\\n\\n## Plan",
        "\\n[A1-R" + rnd + "] proposal text\\n\\n---\\n\\n## Plan",
        1,
    )

if mode not in ("open", "open_then_agree") or (mode == "open_then_agree" and idx == "2"):
    doc = doc.replace("STATUS: OPEN", "STATUS: CONVERGED", 1)
if mode == "slip":
    # Restore skips Dispute Log lines, so slip-mode turns write into the Parking Lot.
    line = tag + " dummy edit by agent " + idx + " with a long trailing explanation of the change"
    doc = doc.replace("## Parking Lot\\n", "## Parking Lot\\n\\n" + line + "\\n", 1)
else:
    doc += "\\n" + tag + " dummy edit by agent " + idx + "\\n"
emit(doc)
'''


class OrchestrateE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.project = Path(self.tmpdir.name) / "project"
        self.project.mkdir()
        self.dummy = self.project / "dummy_agent.py"
        self.dummy.write_text(DUMMY_AGENT)
        self.dummy.chmod(0o755)
        (self.project / "debate.config.json").write_text(
            json.dumps(
                {
                    "aliases": {
                        "dummy1": {
                            "name": "Dummy1",
                            "provider": "d1",
                            "command_template": ["python3", str(self.dummy)],
                            "prompt_transport": "arg",
                            "metering": {"mode": "none"},
                        },
                        "dummy2": {
                            "name": "Dummy2",
                            "provider": "d2",
                            "command_template": ["python3", str(self.dummy)],
                            "prompt_transport": "arg",
                            "metering": {"mode": "none"},
                        },
                        "dummy3": {
                            "name": "Dummy3",
                            "provider": "d3",
                            "command_template": ["python3", str(self.dummy)],
                            "prompt_transport": "arg",
                            "metering": {"mode": "none"},
                        },
                    },
                    "debate": {"default_agents": ["dummy1", "dummy2"], "min_agents": 2, "max_agents": 4},
                }
            )
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def run_orchestrate(self, *args, env_overrides=None):
        env = os.environ.copy()
        env["AGENT_DEBATE_HOST_PROVIDER"] = "none"
        if env_overrides:
            env.update(env_overrides)
        argv = [str(ORCHESTRATE), *args]
        if os.name == "nt":
            argv = ["bash", *argv]
        return subprocess.run(
            argv,
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def debate_file(self):
        files = sorted((self.project / "debates").glob("*.md"))
        self.assertEqual(len(files), 1)
        return files[0]

    def test_full_debate_converges_with_special_character_topic(self):
        result = self.run_orchestrate(
            "--topic", "use X | Y & Z\\ or not?", "--rounds", "2", "--no-plan"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CONVERGED at Round 1", result.stdout)

        text = self.debate_file().read_text()
        self.assertIn("# Debate: use X | Y & Z\\ or not?", text)
        self.assertIn("[A1-R1] dummy edit by agent 1", text)
        self.assertIn("[A2-R1] dummy edit by agent 2", text)

        state = json.loads(self.debate_file().with_suffix("").with_suffix("").parent.joinpath(
            self.debate_file().name.replace(".md", ".state.json")
        ).read_text())
        self.assertEqual(state["status"], "converged")

    def test_alias_rejects_unsafe_codex_pricing(self):
        config_path = self.project / "debate.config.json"
        config = json.loads(config_path.read_text())
        config["aliases"]["dummy1"]["metering"] = {
            "mode": "required",
            "format": "codex_jsonl",
            "pricing": {
                "input_per_million": -1,
                "cached_input_per_million": 0.5,
                "output_per_million": 30,
            },
        }
        config_path.write_text(json.dumps(config))

        result = self.run_orchestrate("--topic", "unsafe pricing", "--rounds", "1", "--no-plan")

        self.assertEqual(result.returncode, 1)
        self.assertIn("codex_jsonl metering requires pricing", result.stderr + result.stdout)

    def test_final_agent_substantive_proposal_change_blocks_convergence(self):
        result = self.run_orchestrate(
            "--topic", "closure review", "--rounds", "1", "--no-plan",
            env_overrides={"DUMMY_MODE": "final_substantive"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("CONVERGED at Round 1", result.stdout)
        state = json.loads(self.debate_file().with_suffix(".state.json").read_text())
        self.assertEqual(state["status"], "max_rounds_reached")

    def state(self):
        return json.loads(self.debate_file().with_suffix(".state.json").read_text())

    def test_confirm_only_turn_closes_a_converged_debate_without_a_full_turn(self):
        result = self.run_orchestrate(
            "--topic", "confirm path", "--rounds", "2", "--no-plan",
            env_overrides={"DUMMY_MODE": "final_substantive"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CONVERGED at Round 2", result.stdout)
        text = self.debate_file().read_text()
        self.assertIn("[A1-R2] Reviewed; no change (confirm-only).", text)
        self.assertNotIn("[A2-R2]", text)
        state = self.state()
        self.assertEqual(state["status"], "converged")
        self.assertIn("confirm", [attempt["phase"] for attempt in state["attempts"]])

    def test_two_agent_debate_converges_in_round_one_when_reviewer_agrees(self):
        result = self.run_orchestrate(
            "--topic", "open then agree", "--rounds", "2", "--no-plan",
            env_overrides={"DUMMY_MODE": "open_then_agree"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CONVERGED at Round 1", result.stdout)
        self.assertNotIn("confirm", [attempt["phase"] for attempt in self.state()["attempts"]])

    def test_objection_to_confirm_falls_back_to_a_full_turn(self):
        result = self.run_orchestrate(
            "--topic", "confirm objection", "--rounds", "2", "--no-plan",
            env_overrides={"DUMMY_MODE": "final_substantive", "DUMMY_CONFIRM": "OBJECT: no"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.debate_file().read_text()
        self.assertIn("[A1-R2] dummy edit by agent 1", text)
        self.assertNotIn("confirm-only", text)

    def test_off_cadence_agent_skips_rounds(self):
        result = self.run_orchestrate(
            "--topic", "cadence", "--rounds", "2", "--no-plan", "--agents", "dummy1,dummy2,dummy3@2",
            env_overrides={"DUMMY_MODE": "open"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.debate_file().read_text()
        self.assertIn("[A3-R1]", text)
        self.assertNotIn("[A3-R2]", text)
        self.assertEqual(self.state()["status"], "max_rounds_reached")
        self.assertEqual(self.state()["resolved_agents"][2]["every"], 2)

    def test_bad_cadence_is_rejected(self):
        result = self.run_orchestrate(
            "--topic", "cadence", "--no-plan", "--agents", "dummy1,dummy2@0",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("must be an integer >= 1", result.stdout + result.stderr)

    def test_one_character_slip_is_restored_not_rejected(self):
        result = self.run_orchestrate(
            "--topic", "slip", "--rounds", "1", "--no-plan",
            env_overrides={"DUMMY_MODE": "slip"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = self.debate_file().read_text()
        self.assertIn("explanation of the change", text)
        self.assertNotIn("explanatiom", text)
        self.assertIn("[A2-R1] dummy edit by agent 2", text)
        self.assertIn("restore", [event["type"] for event in self.state()["events"]])

    def test_pause_file_stops_at_the_next_round_boundary(self):
        env = {"DUMMY_MODE": "open"}
        first = self.run_orchestrate("--topic", "pause", "--rounds", "1", "--no-plan", env_overrides=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        debate_file = self.debate_file()
        pause_file = debate_file.with_suffix(".pause")
        pause_file.write_text("")

        resumed = self.run_orchestrate(
            "--resume", str(debate_file), "--rounds", "2", "--no-plan", env_overrides=env,
        )

        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertFalse(pause_file.exists())
        self.assertEqual(self.state()["status"], "paused")
        text = debate_file.read_text()
        self.assertIn("[A2-R2]", text)
        self.assertNotIn("[A1-R3]", text)

    def test_resumed_host_substantive_change_is_classified_before_review(self):
        config_path = self.project / "debate.config.json"
        config = json.loads(config_path.read_text())
        config["aliases"]["dummy2"]["provider"] = "claude"
        config_path.write_text(json.dumps(config))
        host_env = {"AGENT_DEBATE_HOST_PROVIDER": "claude"}

        first = self.run_orchestrate(
            "--topic", "host closure", "--rounds", "1", "--no-plan", "--skip-provider", "claude",
            env_overrides=host_env,
        )
        self.assertEqual(first.returncode, 2, first.stdout + first.stderr)
        debate_file = self.debate_file()
        text = debate_file.read_text()
        text = text.replace(
            "\n---\n\n## Plan",
            "\n[A2-R1] host proposal condition\n\n---\n\n## Plan",
            1,
        )
        debate_file.write_text(text)

        resumed = self.run_orchestrate(
            "--resume", str(debate_file), "--rounds", "1", "--no-plan", "--skip-provider", "claude",
            env_overrides=host_env,
        )

        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        # A1 already has [A1-R1], so resume does not re-run it; the host's substantive
        # change is unreviewed and must block convergence.
        self.assertNotIn("CONVERGED at Round 1", resumed.stdout)
        state = json.loads(debate_file.with_suffix(".state.json").read_text())
        completed = [
            (event["agent_index"], event["status"])
            for event in state["events"]
            if event.get("type") == "turn"
            and event.get("status") in {"completed", "completed_substantive", "host_completed"}
        ]
        self.assertEqual(completed[-1], (2, "completed_substantive"))
        self.assertEqual(state["status"], "max_rounds_reached")

    def test_history_dropping_agent_is_rejected(self):
        result = self.run_orchestrate(
            "--topic", "preservation check", "--rounds", "2", "--no-plan",
            env_overrides={"DUMMY_MODE": "drop_history"},
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("dropped earlier debate content", result.stdout)
        self.assertIn("dropped earlier turn tags: [A1-R1]", result.stdout)

    def test_rejected_paid_response_is_still_charged(self):
        config = json.loads((self.project / "debate.config.json").read_text())
        for alias in config["aliases"].values():
            alias["metering"] = {"mode": "required", "format": "claude_json"}
        (self.project / "debate.config.json").write_text(json.dumps(config))

        result = self.run_orchestrate(
            "--topic", "metered preservation check", "--rounds", "2", "--no-plan",
            env_overrides={"DUMMY_MODE": "drop_history", "DUMMY_JSON": "1"},
        )

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        state = json.loads(self.debate_file().with_suffix(".state.json").read_text())
        self.assertEqual([attempt["status"] for attempt in state["attempts"]], ["accepted", "rejected"])
        self.assertAlmostEqual(state["total_cost_usd"], 0.02)
        self.assertTrue(state["metering_complete"])

    def test_alias_without_explicit_metering_is_rejected(self):
        config = json.loads((self.project / "debate.config.json").read_text())
        del config["aliases"]["dummy1"]["metering"]
        (self.project / "debate.config.json").write_text(json.dumps(config))

        result = self.run_orchestrate("--topic", "missing metering", "--no-plan")

        self.assertEqual(result.returncode, 1)
        self.assertIn("alias 'dummy1' must declare metering", result.stdout + result.stderr)

    def test_resume_preserves_prior_attempts_and_cost(self):
        config = json.loads((self.project / "debate.config.json").read_text())
        for alias in config["aliases"].values():
            alias["metering"] = {"mode": "required", "format": "claude_json"}
        (self.project / "debate.config.json").write_text(json.dumps(config))
        env = {"DUMMY_MODE": "open", "DUMMY_JSON": "1"}

        first = self.run_orchestrate(
            "--topic", "resume accounting", "--rounds", "1", "--no-plan",
            env_overrides=env,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        debate_file = self.debate_file()
        first_state = json.loads(debate_file.with_suffix(".state.json").read_text())
        self.assertEqual(first_state["status"], "max_rounds_reached", first_state)
        self.assertEqual(first_state["current_round"], 1, first_state)
        second = self.run_orchestrate(
            "--resume", str(debate_file), "--rounds", "1", "--no-plan",
            env_overrides=env,
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        state = json.loads(debate_file.with_suffix(".state.json").read_text())
        self.assertEqual(
            len(state["attempts"]),
            4,
            first.stdout + first.stderr + "\n" + second.stdout + second.stderr + "\n" + repr(first_state) + "\n" + repr(state),
        )
        self.assertAlmostEqual(state["total_cost_usd"], 0.04)

    def test_retry_in_same_round_keeps_both_billable_attempts(self):
        config = json.loads((self.project / "debate.config.json").read_text())
        for alias in config["aliases"].values():
            alias["metering"] = {"mode": "required", "format": "claude_json"}
        (self.project / "debate.config.json").write_text(json.dumps(config))

        first = self.run_orchestrate(
            "--topic", "retry accounting", "--rounds", "1", "--no-plan",
            env_overrides={"DUMMY_MODE": "drop_history", "DUMMY_JSON": "1"},
        )
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        debate_file = self.debate_file()
        second = self.run_orchestrate(
            "--resume", str(debate_file), "--rounds", "1", "--no-plan",
            env_overrides={"DUMMY_JSON": "1"},
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        state = json.loads(debate_file.with_suffix(".state.json").read_text())
        # A1's Round 1 turn already landed in the first run, so resume skips it
        # instead of re-billing it (see detect_resume_round: incomplete rounds
        # are completed with the missing agents, not restarted from A1).
        self.assertEqual(len(state["attempts"]), 3, state)
        self.assertAlmostEqual(state["total_cost_usd"], 0.03)
        self.assertEqual(
            [attempt["attempt_id"] for attempt in state["attempts"]],
            [
                "R1-A1-debate-1",
                "R1-A2-debate-1",
                "R1-A2-debate-2",
            ],
        )

    def test_seeded_resume_completes_round_one(self):
        config = json.loads((self.project / "debate.config.json").read_text())
        for alias in config["aliases"].values():
            alias["metering"] = {"mode": "required", "format": "claude_json"}
        (self.project / "debate.config.json").write_text(json.dumps(config))

        # A1's turn is accepted, A2's turn drops A1's history and is rejected,
        # leaving a file with only [A1-R1] -- as if seeded by hand.
        first = self.run_orchestrate(
            "--topic", "seeded resume", "--rounds", "1", "--no-plan",
            env_overrides={"DUMMY_MODE": "drop_history", "DUMMY_JSON": "1"},
        )
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        debate_file = self.debate_file()
        text = debate_file.read_text()
        self.assertIn("[A1-R1]", text)
        self.assertNotIn("[A2-R1]", text)

        # Simulate a hand-seeded file: no state file to consult.
        debate_file.with_suffix(".state.json").unlink()

        resumed = self.run_orchestrate(
            "--resume", str(debate_file), "--rounds", "1", "--no-plan",
            env_overrides={"DUMMY_JSON": "1"},
        )
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("Resuming at Round 1", resumed.stdout)

        text = debate_file.read_text()
        self.assertNotIn("[A1-R2]", text)

        # The state file was deleted to simulate a hand-seeded resume, so this
        # run's attempts start fresh -- only A2's new (accepted) attempt.
        state = json.loads(debate_file.with_suffix(".state.json").read_text())
        self.assertEqual(
            [attempt["attempt_id"] for attempt in state["attempts"]],
            ["R1-A2-debate-1"],
        )
        preexisting = [
            event for event in state["events"]
            if event.get("type") == "turn" and event.get("status") == "preexisting"
        ]
        self.assertEqual(len(preexisting), 1, state["events"])
        self.assertEqual(preexisting[0]["agent_index"], 1)

    def test_hung_agent_times_out_instead_of_blocking(self):
        hang = self.project / "hang.py"
        hang.write_text("import time\ntime.sleep(60)\n")
        (self.project / "debate.config.json").write_text(
            json.dumps(
                {
                    "aliases": {
                        "h1": {"name": "Dummy1", "provider": "d1",
                               "command_template": ["python3", str(hang)], "prompt_transport": "arg",
                               "metering": {"mode": "none"}},
                        "h2": {"name": "Dummy2", "provider": "d2",
                               "command_template": ["python3", str(hang)], "prompt_transport": "arg",
                               "metering": {"mode": "none"}},
                    },
                    "debate": {"default_agents": ["h1", "h2"], "min_agents": 2, "max_agents": 4},
                }
            )
        )
        result = self.run_orchestrate(
            "--topic", "timeout check", "--rounds", "1", "--no-plan",
            env_overrides={"AGENT_DEBATE_TURN_TIMEOUT": "2"},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out after 2s", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
