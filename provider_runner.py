#!/usr/bin/env python3
"""Run one configured provider and return debate text plus metering metadata."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


class MeteringError(ValueError):
    """Raised when required provider accounting is missing or malformed."""

    def __init__(self, message: str, metering: dict[str, Any] | None = None):
        super().__init__(message)
        self.metering = metering


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MeteringError(f"invalid {field} usage")
    return value


def _usage(raw: Any, *, codex_total_input: bool = False) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise MeteringError("missing usage")
    input_tokens = _non_negative_int(raw.get("input_tokens"), "input_tokens")
    cached_tokens = _non_negative_int(
        raw.get("cached_input_tokens", raw.get("cache_read_input_tokens", 0)),
        "cached_input_tokens",
    )
    output_tokens = _non_negative_int(raw.get("output_tokens"), "output_tokens")
    if codex_total_input:
        if cached_tokens > input_tokens:
            raise MeteringError("cached_input_tokens exceeds input_tokens usage")
        input_tokens -= cached_tokens
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
    }


def _cost_from_pricing(usage: dict[str, int], pricing: Any) -> float:
    if not isinstance(pricing, dict):
        raise MeteringError("missing pricing")
    keys = (
        "input_per_million",
        "cached_input_per_million",
        "output_per_million",
    )
    if any(
        not isinstance(pricing.get(key), (int, float))
        or isinstance(pricing.get(key), bool)
        or not math.isfinite(float(pricing[key]))
        or pricing[key] < 0
        for key in keys
    ):
        raise MeteringError("invalid pricing")
    return (
        usage["input_tokens"] * float(pricing["input_per_million"])
        + usage["cached_input_tokens"] * float(pricing["cached_input_per_million"])
        + usage["output_tokens"] * float(pricing["output_per_million"])
    ) / 1_000_000


def _parse_claude_json(raw: str) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MeteringError(f"invalid Claude JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MeteringError("invalid Claude JSON result")
    usage = _usage(payload.get("usage"))
    cost = payload.get("total_cost_usd")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
        raise MeteringError("Claude JSON missing total_cost_usd")
    metering = {"status": "complete", "usage": usage, "cost_usd": float(cost)}
    content = payload.get("result")
    if not isinstance(content, str) or not content.strip():
        raise MeteringError("Claude JSON missing result content", metering)
    return content, metering


def _parse_codex_jsonl(raw: str, pricing: Any) -> tuple[str, dict[str, Any]]:
    content = None
    raw_usage = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MeteringError(f"invalid Codex JSONL: {exc}") from exc
        if not isinstance(event, dict):
            continue
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                candidate = item.get("text")
                if isinstance(candidate, str) and candidate.strip():
                    content = candidate
        elif event.get("type") == "turn.completed":
            raw_usage = event.get("usage")
    usage = _usage(raw_usage, codex_total_input=True)
    metering = {
        "status": "complete",
        "usage": usage,
        "cost_usd": _cost_from_pricing(usage, pricing),
    }
    if content is None:
        raise MeteringError("Codex JSONL missing assistant content", metering)
    return content, metering


def _copilot_content(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("type") == "assistant.message" and isinstance(payload.get("data"), dict):
        content = payload["data"].get("content") or payload["data"].get("text")
        if isinstance(content, str) and content.strip():
            return content
    if str(payload.get("role", "")).lower() == "assistant":
        content = payload.get("content") or payload.get("text")
        if isinstance(content, str) and content.strip():
            return content
    message = payload.get("message")
    if isinstance(message, dict):
        content = _copilot_content(message)
        if content:
            return content
    messages = payload.get("messages")
    if isinstance(messages, list):
        for candidate in reversed(messages):
            content = _copilot_content(candidate)
            if content:
                return content
    for choice in payload.get("choices", []) if isinstance(payload.get("choices"), list) else []:
        content = _copilot_content(choice.get("message") if isinstance(choice, dict) else None)
        if content:
            return content
    return _copilot_content(payload.get("result"))


def _parse_copilot_json(raw: str) -> str:
    content = None
    try:
        content = _copilot_content(json.loads(raw))
    except json.JSONDecodeError:
        for line in raw.splitlines():
            try:
                candidate = _copilot_content(json.loads(line))
            except json.JSONDecodeError:
                continue
            if candidate:
                content = candidate
    if not content:
        raise MeteringError("Copilot JSON output missing assistant content")
    return content


def parse_provider_output(raw: str, metering: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract assistant content and normalized accounting from provider output."""
    if not isinstance(metering, dict):
        raise MeteringError("missing metering configuration")
    mode = metering.get("mode")
    if mode == "none":
        content = _parse_copilot_json(raw) if metering.get("format") == "copilot_json" else raw
        return content, {"status": "not_applicable", "usage": None, "cost_usd": None}
    if mode != "required":
        raise MeteringError("metering.mode must be 'required' or 'none'")

    output_format = metering.get("format")
    if output_format == "claude_json":
        return _parse_claude_json(raw)
    if output_format == "codex_jsonl":
        return _parse_codex_jsonl(raw, metering.get("pricing"))
    raise MeteringError("unsupported required metering format")


def _provider_environment(provider: str) -> dict[str, str]:
    env = os.environ.copy()
    if provider == "claude":
        env.pop("CLAUDECODE", None)
        for key in list(env):
            if key.startswith("CLAUDE_CODE_"):
                env.pop(key, None)
    elif provider == "codex":
        env.pop("CODEX_THREAD_ID", None)
    elif provider == "gemini":
        env.pop("GEMINI_SESSION_ID", None)
        env.pop("GEMINI_CLI_SESSION", None)
    return env


def run(config_path: Path, agent_index: int, prompt_path: Path, metering_path: Path) -> int:
    config = json.loads(config_path.read_text())
    agents = config.get("agents", [])
    if agent_index < 1 or agent_index > len(agents):
        raise ValueError(f"invalid agent index {agent_index}")
    agent = agents[agent_index - 1]
    command = agent.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError("invalid agent command")
    # Windows CLIs are .cmd shims; shutil.which applies PATHEXT so subprocess can find them.
    here = str(Path(__file__).resolve().parent)
    command = [str(part).replace("{SCRIPT_DIR}", here) for part in command]
    command = [shutil.which(command[0]) or command[0], *command[1:]]
    transport = agent.get("prompt_transport", "arg")
    provider = str(agent.get("provider", "")).strip().lower()
    metering_spec = agent.get("metering")
    unmetered = isinstance(metering_spec, dict) and metering_spec.get("mode") == "none"
    prompt = prompt_path.read_text()
    timeout = int(os.environ.get("AGENT_DEBATE_TURN_TIMEOUT", "900"))

    try:
        if transport == "stdin":
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                env=_provider_environment(provider),
                timeout=timeout,
            )
        else:
            result = subprocess.run(
                command + [prompt],
                capture_output=True,
                text=True,
                env=_provider_environment(provider),
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        metering_path.write_text(json.dumps({
            "status": "not_applicable" if unmetered else "failed",
            "usage": None,
            "cost_usd": None,
            "error": f"timed out after {timeout}s",
        }))
        print(f"Error: agent command timed out after {timeout}s", file=sys.stderr)
        return 124

    if result.returncode != 0:
        try:
            _, failure_metering = parse_provider_output(result.stdout, metering_spec)
        except MeteringError as exc:
            failure_metering = exc.metering or {
                "status": "not_applicable" if unmetered else "failed",
                "usage": None,
                "cost_usd": None,
            }
        metering_path.write_text(json.dumps({
            **failure_metering,
            "failure_kind": "provider_failed",
            "error": "provider command failed",
        }))
        if result.stderr:
            sys.stderr.write(result.stderr)
        return result.returncode

    try:
        content, normalized = parse_provider_output(result.stdout, metering_spec)
    except MeteringError as exc:
        failed_metering = exc.metering or {"status": "failed", "usage": None, "cost_usd": None}
        metering_path.write_text(json.dumps({**failed_metering, "error": str(exc)}))
        print(f"Error: metering failed: {exc}", file=sys.stderr)
        return 1

    metering_path.write_text(json.dumps(normalized))
    sys.stdout.write(content)
    return 0


def main() -> int:
    if len(sys.argv) != 5:
        print("Usage: provider_runner.py CONFIG AGENT_INDEX PROMPT METERING_OUT", file=sys.stderr)
        return 2
    try:
        return run(Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
