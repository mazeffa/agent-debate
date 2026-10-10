import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from provider_runner import MeteringError, parse_provider_output, run


class ProviderOutputTest(unittest.TestCase):
    def test_claude_json_returns_content_usage_and_provider_cost(self):
        raw = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "# Debate: metered\n",
                "total_cost_usd": 0.123456,
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 40,
                    "output_tokens": 20,
                },
            }
        )

        content, metering = parse_provider_output(
            raw,
            {"mode": "required", "format": "claude_json"},
        )

        self.assertEqual(content, "# Debate: metered\n")
        self.assertEqual(
            metering["usage"],
            {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20},
        )
        self.assertEqual(metering["cost_usd"], 0.123456)
        self.assertEqual(metering["status"], "complete")

    def test_codex_jsonl_returns_last_message_and_computed_cost(self):
        raw = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "abc"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "agent_message",
                            "text": "# Debate: metered\n",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 40,
                            "output_tokens": 20,
                        },
                    }
                ),
            ]
        )
        spec = {
            "mode": "required",
            "format": "codex_jsonl",
            "pricing": {
                "input_per_million": 5.0,
                "cached_input_per_million": 0.5,
                "output_per_million": 30.0,
            },
        }

        content, metering = parse_provider_output(raw, spec)

        self.assertEqual(content, "# Debate: metered\n")
        self.assertEqual(
            metering["usage"],
            {"input_tokens": 60, "cached_input_tokens": 40, "output_tokens": 20},
        )
        self.assertAlmostEqual(metering["cost_usd"], 0.00092)

    def test_required_metering_rejects_missing_usage(self):
        raw = json.dumps({"type": "result", "result": "# Debate: unmetered\n"})

        with self.assertRaisesRegex(MeteringError, "usage"):
            parse_provider_output(raw, {"mode": "required", "format": "claude_json"})

    def test_invalid_claude_content_still_exposes_billable_metering(self):
        raw = json.dumps(
            {
                "type": "result",
                "result": "",
                "total_cost_usd": 0.25,
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 40, "output_tokens": 1},
            }
        )

        with self.assertRaisesRegex(MeteringError, "content") as raised:
            parse_provider_output(raw, {"mode": "required", "format": "claude_json"})

        self.assertEqual(raised.exception.metering["cost_usd"], 0.25)
        self.assertEqual(raised.exception.metering["status"], "complete")

    def test_codex_metering_rejects_missing_pricing(self):
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "# Debate: unpriced\n"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
                    }
                ),
            ]
        )

        with self.assertRaisesRegex(MeteringError, "pricing"):
            parse_provider_output(raw, {"mode": "required", "format": "codex_jsonl"})

    def test_invalid_codex_content_still_exposes_billable_metering(self):
        raw = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 20},
            }
        )
        spec = {
            "mode": "required",
            "format": "codex_jsonl",
            "pricing": {
                "input_per_million": 5.0,
                "cached_input_per_million": 0.5,
                "output_per_million": 30.0,
            },
        }

        with self.assertRaisesRegex(MeteringError, "content") as raised:
            parse_provider_output(raw, spec)

        self.assertEqual(
            raised.exception.metering["usage"],
            {"input_tokens": 60, "cached_input_tokens": 40, "output_tokens": 20},
        )
        self.assertAlmostEqual(raised.exception.metering["cost_usd"], 0.00092)

    def test_codex_metering_rejects_unsafe_prices(self):
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "# Debate: unsafe price\n"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
                    }
                ),
            ]
        )
        for unsafe in (-1, True, float("nan"), float("inf")):
            with self.subTest(unsafe=unsafe):
                spec = {
                    "mode": "required",
                    "format": "codex_jsonl",
                    "pricing": {
                        "input_per_million": unsafe,
                        "cached_input_per_million": 0.5,
                        "output_per_million": 30.0,
                    },
                }
                with self.assertRaisesRegex(MeteringError, "pricing"):
                    parse_provider_output(raw, spec)

    def test_explicitly_unmetered_output_is_not_reported_as_zero_cost(self):
        content, metering = parse_provider_output("plain response", {"mode": "none"})

        self.assertEqual(content, "plain response")
        self.assertEqual(metering["status"], "not_applicable")
        self.assertIsNone(metering["cost_usd"])

    def test_unmetered_copilot_json_still_extracts_assistant_content(self):
        raw = json.dumps(
            {
                "type": "assistant.message",
                "data": {"content": "# Debate: copilot\n"},
            }
        )

        content, metering = parse_provider_output(
            raw,
            {"mode": "none", "format": "copilot_json"},
        )

        self.assertEqual(content, "# Debate: copilot\n")
        self.assertEqual(metering["status"], "not_applicable")

    def test_unmetered_copilot_messages_envelope_remains_supported(self):
        raw = json.dumps(
            {"messages": [{"role": "assistant", "content": "# Debate: messages\n"}]}
        )

        content, _ = parse_provider_output(
            raw,
            {"mode": "none", "format": "copilot_json"},
        )

        self.assertEqual(content, "# Debate: messages\n")

    def test_failed_provider_process_preserves_structured_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "provider.py"
            provider.write_text(
                "import json, sys\n"
                "print(json.dumps({'type': 'result', 'result': 'provider error', "
                "'total_cost_usd': 0.07, 'usage': {'input_tokens': 10, 'output_tokens': 2}}))\n"
                "raise SystemExit(7)\n"
            )
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "command": ["python3", str(provider)],
                                "prompt_transport": "arg",
                                "provider": "claude",
                                "metering": {"mode": "required", "format": "claude_json"},
                            }
                        ]
                    }
                )
            )
            prompt = root / "prompt.txt"
            prompt.write_text("prompt")
            metering_file = root / "metering.json"

            return_code = run(config, 1, prompt, metering_file)

            self.assertEqual(return_code, 7)
            metering = json.loads(metering_file.read_text())
            self.assertEqual(metering["status"], "complete")
            self.assertEqual(metering["cost_usd"], 0.07)
            self.assertEqual(metering["failure_kind"], "provider_failed")

    def test_turn_log_env_var_dumps_prompt_stdout_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = root / "provider.py"
            provider.write_text(
                "import json, sys\n"
                "sys.stderr.write('warn\\n')\n"
                "print(json.dumps({'type': 'result', 'result': 'hello', "
                "'total_cost_usd': 0.01, 'usage': {'input_tokens': 1, 'output_tokens': 1}}))\n"
            )
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "agents": [
                            {
                                "command": ["python3", str(provider)],
                                "prompt_transport": "arg",
                                "provider": "claude",
                                "metering": {"mode": "required", "format": "claude_json"},
                            }
                        ]
                    }
                )
            )
            prompt = root / "prompt.txt"
            prompt.write_text("the prompt text")
            metering_file = root / "metering.json"
            log_prefix = str(root / "turns" / "R1-A1-proposal-1")

            with mock.patch.dict(os.environ, {"AGENT_DEBATE_TURN_LOG": log_prefix}):
                return_code = run(config, 1, prompt, metering_file)

            self.assertEqual(return_code, 0)
            self.assertEqual(Path(log_prefix + ".prompt.txt").read_text(), "the prompt text")
            self.assertEqual(Path(log_prefix + ".stdout.txt").read_text().strip(), json.dumps({
                "type": "result", "result": "hello",
                "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 1},
            }))
            self.assertIn("warn", Path(log_prefix + ".stderr.txt").read_text())


if __name__ == "__main__":
    unittest.main()
