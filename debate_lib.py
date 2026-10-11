#!/usr/bin/env python3
"""Debate-file parsing and rendering helpers for orchestrate.sh.

Commands:
  proposal-converged FILE      exit 0 if the Proposal section says STATUS: CONVERGED
  plan-converged FILE          exit 0 if the Plan section says PLAN_STATUS: CONVERGED
  open-disputes FILE           exit 0 if any Dispute Log row has status OPEN
  verify-preservation OLD NEW  exit 0 if NEW keeps all OLD turn tags and dispute rows
  restore-rewritten OLD NEW    put back earlier tagged lines NEW slightly rewrote; exit 0 if NEW then passes
  substantive-change OLD NEW   exit 0 if core debate content changed
  ended-converged FILE         exit 0 if the Proposal says STATUS: CONVERGED
  add-note FILE TEXT           add TEXT as a line under ## Parking Lot
  unified-diff OLD NEW         print a unified diff of OLD to NEW
  events-converged STATE N     exit 0 if every agent 1..N agrees with the latest substantive change
  agent-agrees STATE IDX       exit 0 if agent IDX already agrees with the latest substantive change
  render TEMPLATE OUT          render the debate template using DL_* env vars
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
from collections import Counter

RESTORE_MIN_RATIO = 0.85
RESTORE_MIN_LEN = 40
# Turn events meaning the agent read the document and edited it or signed off.
COUNTED_TURNS = {"completed", "completed_substantive", "host_completed", "confirmed"}

# A tagged "nothing to change" note; it must not count as a substantive edit.
NO_CHANGE_NOTE = re.compile(r"^[-*]?\s*\[A\d+-R\d+\]\s*(reviewed|no change)\b", re.IGNORECASE)


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def section_body(text: str, heading: str) -> str | None:
    """Return the lines under `heading` up to the next '## ' heading."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i + 1
            break
    if start is None:
        return None
    body = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def section_has_status_line(text: str, heading: str, status_line: str) -> bool:
    body = section_body(text, heading)
    if body is None:
        return False
    return any(line.strip() == status_line for line in body.splitlines())


def dispute_rows(text: str) -> list[list[str]]:
    """Data rows of the Dispute Log table. Tolerates a missing trailing pipe."""
    body = section_body(text, "## Dispute Log")
    if body is None:
        return []
    rows = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not any(cells):
            continue
        if cells and cells[0] == "Round":
            continue
        if all(set(c) <= {"-", ":", " "} for c in cells if c):
            continue
        rows.append(cells)
    return rows


def has_open_disputes(text: str) -> bool:
    for cells in dispute_rows(text):
        non_empty = [c for c in cells if c]
        if non_empty and non_empty[-1] == "OPEN":
            return True
    return False


def collect_turn_tags(text: str) -> dict[str, int]:
    """Occurrence counts of well-formed turn tags like [A1-R2].

    Counts matter: the template and quoted text can mention a tag, so a
    set would miss an agent deleting one real occurrence of a duplicate.
    """
    tags: dict[str, int] = {}
    i = 0
    while True:
        i = text.find("[A", i)
        if i == -1:
            break
        end = text.find("]", i)
        if end == -1:
            break
        if end - i <= 10:
            body = text[i + 2 : end]
            agent_num, sep, round_num = body.partition("-R")
            if sep and agent_num.isdigit() and round_num.isdigit():
                tag = text[i : end + 1]
                tags[tag] = tags.get(tag, 0) + 1
        i += 2
    return tags


def tagged_lines(text: str) -> list[tuple[int, str, str, bool]]:
    """(line index, raw line, canonical line, in Dispute Log) for each tagged line.

    The canonical form ignores only allowed review-state markup changes.
    """
    out = []
    in_dispute_log = False
    statuses = {"OPEN", "CLOSED", "PARKED"}

    for index, raw_line in enumerate(text.splitlines()):
        if raw_line.startswith("## "):
            in_dispute_log = raw_line.strip() == "## Dispute Log"
        if not collect_turn_tags(raw_line):
            continue

        line = raw_line.replace("~~", "").strip()
        if in_dispute_log and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and cells[-1] in statuses:
                cells[-1] = "<STATUS>"
            line = "| " + " | ".join(cells) + " |"
        out.append((index, raw_line, line, in_dispute_log))
    return out


def canonical_tagged_lines(text: str) -> Counter[str]:
    return Counter(canon for _, _, canon, _ in tagged_lines(text))


def _canonical_core_sections(text: str) -> tuple[str, ...]:
    sections = []
    for heading in ("## Assumptions", "## Success Criteria", "## Proposal"):
        body = section_body(text, heading) or ""
        normalized = []
        for line in body.splitlines():
            stripped = line.strip()
            if heading == "## Proposal" and stripped in {"STATUS: OPEN", "STATUS: CONVERGED"}:
                continue
            if NO_CHANGE_NOTE.match(stripped):
                continue
            normalized.append(line.rstrip())
        sections.append("\n".join(normalized).strip())
    return tuple(sections)


def has_substantive_change(old_text: str, new_text: str) -> bool:
    """Whether assumptions, success criteria, or proposal substance changed."""
    return _canonical_core_sections(old_text) != _canonical_core_sections(new_text)


def preservation_problems(old_text: str, new_text: str) -> list[str]:
    problems = []
    old_tags = collect_turn_tags(old_text)
    new_tags = collect_turn_tags(new_text)
    dropped = sorted(tag for tag, count in old_tags.items() if new_tags.get(tag, 0) < count)
    if dropped:
        problems.append(f"dropped earlier turn tags: {', '.join(dropped)}")

    old_lines = canonical_tagged_lines(old_text)
    new_lines = canonical_tagged_lines(new_text)
    rewritten = old_lines - new_lines
    if rewritten:
        tags = sorted(collect_turn_tags("\n".join(rewritten.elements())))
        suffix = f": {', '.join(tags)}" if tags else ""
        problems.append(f"rewrote earlier tagged lines{suffix}")
        problems.extend(f"  missing original line: {line}" for line in sorted(rewritten.elements()))

    old_rows = len(dispute_rows(old_text))
    new_rows = len(dispute_rows(new_text))
    if new_rows < old_rows:
        problems.append(f"dispute log shrank from {old_rows} to {new_rows} rows")
    return problems


def cmd_verify_preservation(old_path: str, new_path: str) -> int:
    problems = preservation_problems(read_text(old_path), read_text(new_path))
    for problem in problems:
        print(problem)
    return 1 if problems else 0


def cmd_restore_rewritten(old_path: str, new_path: str) -> int:
    """Swap slightly rewritten earlier tagged lines back to their original text.

    Restores only a one-to-one match with the same tag set and a near-identical
    body; dropped tags, a shrunken dispute log or any ambiguity exits 1 and leaves
    NEW untouched.
    shortcut: Dispute Log rows and a rewritten line's strikethrough state are not
    restored; upgrade if either is observed.
    """
    old_text = read_text(old_path)
    new_text = read_text(new_path)
    problems = preservation_problems(old_text, new_text)
    if not problems:
        return 0

    def fail() -> int:
        print("\n".join(problems))
        return 1

    if any(p.startswith(("dropped", "dispute log shrank")) for p in problems):
        return fail()

    old_lines = tagged_lines(old_text)
    new_lines = tagged_lines(new_text)
    missing = Counter(c for _, _, c, _ in old_lines) - Counter(c for _, _, c, _ in new_lines)
    extra = Counter(c for _, _, c, _ in new_lines) - Counter(c for _, _, c, _ in old_lines)
    old_raw = {c: raw for _, raw, c, in_log in old_lines if not in_log}
    candidates = [(i, c) for i, _, c, in_log in new_lines if not in_log and extra[c] > 0]

    swaps: dict[int, str] = {}
    for line in missing.elements():
        tags = set(collect_turn_tags(line))
        matches = [
            i for i, c in candidates
            if i not in swaps
            and set(collect_turn_tags(c)) == tags
            and difflib.SequenceMatcher(None, line, c).ratio() >= RESTORE_MIN_RATIO
        ]
        if line not in old_raw or len(line) < RESTORE_MIN_LEN or len(matches) != 1:
            return fail()
        swaps[matches[0]] = old_raw[line]

    lines = new_text.splitlines()
    for i, original in swaps.items():
        lines[i] = original
    restored = "\n".join(lines) + "\n"
    remaining = preservation_problems(old_text, restored)
    if remaining:
        problems = remaining
        return fail()
    with open(new_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(restored)
    for original in swaps.values():
        print(f"restored: {original.strip()}")
    return 0


def cmd_add_note(path: str, note: str) -> int:
    lines = read_text(path).splitlines()
    heading = next((i for i, l in enumerate(lines) if l.strip() == "## Parking Lot"), None)
    if heading is None:
        at = next((i for i, l in enumerate(lines) if l.strip() == "## Dispute Log"), len(lines))
        lines[at:at] = ["## Parking Lot", "", note, ""]
    else:
        end = next((i for i in range(heading + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        while end > heading + 1 and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, note)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return 0


def agreeing_agents(events: list[dict]) -> tuple[set, bool]:
    """(agents that agree with the latest substantive change, whether one exists).

    The author of the latest substantive edit agrees. After it, an agent agrees if it
    confirmed, or took a turn that ended with STATUS: CONVERGED (`ended_converged`).
    `preexisting` events carry no log position, so they count only when there is no
    substantive edit at all, where any turn counts.
    """
    turns = [
        e for e in events
        if e.get("type") == "turn" and e.get("status") in COUNTED_TURNS | {"preexisting"}
    ]
    barrier = next(
        (i for i in range(len(turns) - 1, -1, -1) if turns[i].get("status") == "completed_substantive"),
        None,
    )
    if barrier is None:
        return {e.get("agent_index") for e in turns}, False
    return {turns[barrier].get("agent_index")} | {
        e.get("agent_index") for e in turns[barrier + 1:]
        if e.get("status") == "confirmed" or e.get("ended_converged")
    }, True


def events_converged(events: list[dict], agent_count: int) -> bool:
    return agreeing_agents(events)[0] >= set(range(1, agent_count + 1))


def cmd_render(template_path: str, out_path: str) -> int:
    text = read_text(template_path)

    topic = os.environ.get("DL_TOPIC", "")
    date = os.environ.get("DL_DATE", "")
    max_rounds = os.environ.get("DL_MAX_ROUNDS", "")
    constraints = os.environ.get("DL_CONSTRAINTS", "").strip()
    agent_count = int(os.environ.get("DL_AGENT_COUNT", "2"))
    agents = [
        os.environ.get("DL_AGENT1", ""),
        os.environ.get("DL_AGENT2", ""),
        os.environ.get("DL_AGENT3", ""),
        os.environ.get("DL_AGENT4", ""),
    ]
    files = [f for f in os.environ.get("DL_FILES", "").splitlines() if f.strip()]

    if files:
        file_context = "\n".join(f"- `{f.strip()}`" for f in files)
    else:
        file_context = "None specified — agents should explore as needed."

    replacements = {
        "{TOPIC}": topic,
        "{DATE}": date,
        "{MAX_ROUNDS}": max_rounds,
        "{PROBLEM_DESCRIPTION}": topic,
        "{FILE_LIST_WITH_KEY_SECTIONS}": file_context,
        "{ANY_CONSTRAINTS_OR_NON_NEGOTIABLES}": constraints or "None specified.",
        "{AGENT_1_NAME}": agents[0],
        "{AGENT_2_NAME}": agents[1],
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)

    lines = []
    for line in text.splitlines():
        if "{AGENT_3_NAME}" in line:
            if agent_count < 3:
                continue
            line = line.replace("{AGENT_3_NAME}", agents[2])
        if "{AGENT_4_NAME}" in line:
            if agent_count < 4:
                continue
            line = line.replace("{AGENT_4_NAME}", agents[3])
        lines.append(line)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    command = sys.argv[1]
    args = sys.argv[2:]

    if command == "proposal-converged" and len(args) == 1:
        return 0 if section_has_status_line(read_text(args[0]), "## Proposal", "STATUS: CONVERGED") else 1
    if command == "plan-converged" and len(args) == 1:
        return 0 if section_has_status_line(read_text(args[0]), "## Plan", "PLAN_STATUS: CONVERGED") else 1
    if command == "open-disputes" and len(args) == 1:
        return 0 if has_open_disputes(read_text(args[0])) else 1
    if command == "verify-preservation" and len(args) == 2:
        return cmd_verify_preservation(args[0], args[1])
    if command == "substantive-change" and len(args) == 2:
        return 0 if has_substantive_change(read_text(args[0]), read_text(args[1])) else 1
    if command == "restore-rewritten" and len(args) == 2:
        return cmd_restore_rewritten(args[0], args[1])
    if command == "ended-converged" and len(args) == 1:
        return 0 if section_has_status_line(read_text(args[0]), "## Proposal", "STATUS: CONVERGED") else 1
    if command == "add-note" and len(args) == 2:
        return cmd_add_note(args[0], args[1])
    if command == "unified-diff" and len(args) == 2:
        sys.stdout.write("".join(difflib.unified_diff(
            read_text(args[0]).splitlines(keepends=True),
            read_text(args[1]).splitlines(keepends=True),
            "before", "after",
        )))
        return 0
    if command == "events-converged" and len(args) == 2:
        with open(args[0], "r", encoding="utf-8") as f:
            events = json.load(f).get("events", [])
        return 0 if events_converged(events, int(args[1])) else 1
    if command == "agent-agrees" and len(args) == 2:
        with open(args[0], "r", encoding="utf-8") as f:
            events = json.load(f).get("events", [])
        agreeing, has_barrier = agreeing_agents(events)
        return 0 if has_barrier and int(args[1]) in agreeing else 1
    if command == "render" and len(args) == 2:
        return cmd_render(args[0], args[1])

    print(f"Error: unknown command or wrong arguments: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
