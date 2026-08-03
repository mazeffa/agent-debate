#!/usr/bin/env python3
"""Persist idempotent per-attempt debate usage and cost."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _recompute(state: dict[str, Any]) -> None:
    usage_totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
    }
    total_cost = 0.0
    complete = True
    for attempt in state.get("attempts", []):
        if attempt.get("metering_status") == "failed":
            complete = False
        usage = attempt.get("usage")
        if isinstance(usage, dict):
            for key in usage_totals:
                usage_totals[key] += int(usage.get(key, 0))
        cost = attempt.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total_cost += float(cost)
    state["usage_totals"] = usage_totals
    state["total_cost_usd"] = round(total_cost, 12)
    state["metering_complete"] = complete


def record_attempt(state_path: str | Path, attempt: dict[str, Any]) -> None:
    """Upsert one attempt by ID and recompute aggregates from source records."""
    attempt_id = attempt.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id is required")
    path = Path(state_path)
    state = json.loads(path.read_text()) if path.exists() else {"events": []}
    attempts = state.setdefault("attempts", [])
    for index, existing in enumerate(attempts):
        if existing.get("attempt_id") == attempt_id:
            attempts[index] = attempt
            break
    else:
        attempts.append(attempt)
    _recompute(state)
    path.write_text(json.dumps(state, indent=2) + "\n")
