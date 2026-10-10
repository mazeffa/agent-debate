#!/usr/bin/env bash
# orchestrate.sh — Runs an agent debate via shared markdown file
#
# Usage:
#   ./orchestrate.sh --topic "question" --rounds 3
#   ./orchestrate.sh --topic "question" --agents opus,codex,sonnet --rounds 2
#   ./orchestrate.sh --resume path/to/debate.md --rounds 2

set -euo pipefail

# Windows Python writes CRLF into pipes and cp1252 to stdout; normalize once here, not at 21 call sites.
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) export PYTHONUTF8=1; python3() { command python3 "$@" | tr -d '\r'; } ;; esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(pwd)"
DEBATES_DIR="$PROJECT_DIR/debates"
GUARDRAILS="$SCRIPT_DIR/agent-guardrails.md"
TEMPLATE="$SCRIPT_DIR/TEMPLATE.md"

# Defaults
MAX_ROUNDS=3
TOPIC=""
FILES=""
RESUME_FILE=""
CONSTRAINTS=""
AGENT_1="Claude"
AGENT_2="Codex"
AGENT1_OVERRIDE_NAME=""
AGENT2_OVERRIDE_NAME=""
AGENT3_OVERRIDE_NAME=""
AGENT4_OVERRIDE_NAME=""
CONFIG_FILE=""
AGENTS_SPEC=""
RUNTIME_CONFIG_FILE=""
AGENT_COUNT=0
declare -a AGENT_NAMES=()
declare -a AGENT_PROVIDERS=()
HOST_PROVIDER=""
SKIP_PROVIDER=""
STATE_FILE=""
PLAN_MODE=true
PLAN_ROUNDS=2

cleanup() {
  if [[ -n "${RUNTIME_CONFIG_FILE:-}" && -f "${RUNTIME_CONFIG_FILE:-}" ]]; then
    rm -f "$RUNTIME_CONFIG_FILE"
  fi
}

trap cleanup EXIT

PROVIDER_PID=""

kill_provider_tree() {
  local pid="$PROVIDER_PID"
  [[ -n "$pid" ]] || return 0
  if [[ -r "/proc/$pid/winpid" ]]; then
    # Windows: kill python and the agent CLI it spawned.
    taskkill //F //T //PID "$(cat "/proc/$pid/winpid")" >/dev/null 2>&1
  else
    pkill -TERM -P "$pid" 2>/dev/null
    kill -TERM "$pid" 2>/dev/null
  fi
  PROVIDER_PID=""
}

on_stop() {
  trap - INT TERM
  kill_provider_tree
  if [[ -n "${STATE_FILE:-}" && -f "$STATE_FILE" ]]; then
    state_set_status "stopped" "Stopped by signal; resume picks the round from turn tags" "${round:-${CURRENT_ROUND:-1}}"
  fi
  exit 130
}

trap on_stop INT TERM

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Start a new debate:
  --topic "question"          The debate topic (required for new debates)
  --files file1 file2 ...     Relevant source files to include as context
  --constraints "text"        Non-negotiable constraints
  --agents a,b[,c[,d...]]     Agent aliases/models (e.g. opus,codex or gemini,codex,sonnet,copilot)
  --config path/to/config.json  Agent configuration file (optional)
  --skip-provider name        Optional: skip one provider (claude|codex|gemini|copilot) so you can take its turns yourself
                             Skips every configured agent on that provider (opus, sonnet, fable
                             are all "claude"); two agents that share a provider cannot be
                             separated with this flag.
                             For Copilot host sessions, set AGENT_DEBATE_HOST_PROVIDER=copilot
  --rounds N                  Max rounds to run (ceiling, not quota; debate may converge earlier)
  --agent1 name               Override Agent 1 display name
  --agent2 name               Override Agent 2 display name
  --agent3 name               Override Agent 3 display name
  --agent4 name               Override Agent 4 display name
  --plan                      Force-enable Plan phase (default: enabled)
  --no-plan                   Disable Plan phase for this run
  --plan-rounds N             Max plan review rounds (default: 2)

Resume an existing debate:
  --resume path/to/debate.md  Path to existing debate file. Continues the debate: if the highest
                             round in the file is missing turns from some agents, those agents
                             take their turns first; otherwise the next round starts.
  --rounds N                  Max rounds to run (ceiling, not quota; debate may converge earlier)

Exit codes:
  0  Success (debate completed)
  1  Failure
  2  Paused for host-direct turn (missing required host tag)

EOF
  exit 1
}

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --topic) TOPIC="$2"; shift 2 ;;
    --files) shift; FILES=""; while [[ $# -gt 0 && ! "$1" =~ ^-- ]]; do FILES="$FILES $1"; shift; done ;;
    --constraints) CONSTRAINTS="$2"; shift 2 ;;
    --agents) AGENTS_SPEC="$2"; shift 2 ;;
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --skip-provider) SKIP_PROVIDER="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
    --rounds) MAX_ROUNDS="$2"; shift 2 ;;
    --resume) RESUME_FILE="$2"; shift 2 ;;
    --agent1) AGENT1_OVERRIDE_NAME="$2"; shift 2 ;;
    --agent2) AGENT2_OVERRIDE_NAME="$2"; shift 2 ;;
    --agent3) AGENT3_OVERRIDE_NAME="$2"; shift 2 ;;
    --agent4) AGENT4_OVERRIDE_NAME="$2"; shift 2 ;;
    --plan) PLAN_MODE=true; shift ;;
    --no-plan) PLAN_MODE=false; shift ;;
    --plan-rounds) PLAN_ROUNDS="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

# Validate
if [[ -z "$RESUME_FILE" && -z "$TOPIC" ]]; then
  echo "Error: --topic is required for new debates (or use --resume)"
  usage
fi

resolve_config() {
  if [[ -n "$CONFIG_FILE" ]]; then
    if [[ ! -f "$CONFIG_FILE" ]]; then
      echo "Error: config file not found: $CONFIG_FILE"
      exit 1
    fi
    return
  fi

  if [[ -f "./debate.config.json" ]]; then
    CONFIG_FILE="./debate.config.json"
  elif [[ -f "$HOME/.agent-debate/config.json" ]]; then
    CONFIG_FILE="$HOME/.agent-debate/config.json"
  else
    CONFIG_FILE=""
  fi
}

detect_host_provider() {
  if [[ -n "${AGENT_DEBATE_HOST_PROVIDER:-}" ]]; then
    HOST_PROVIDER="$(printf '%s' "$AGENT_DEBATE_HOST_PROVIDER" | tr '[:upper:]' '[:lower:]')"
    return
  fi

  if [[ -n "${CODEX_THREAD_ID:-}" ]]; then
    HOST_PROVIDER="codex"
  elif [[ -n "${CLAUDE_CODE_SSE_PORT:-}" || -n "${CLAUDECODE:-}" ]]; then
    HOST_PROVIDER="claude"
  elif [[ -n "${GEMINI_CLI_SESSION:-}" || -n "${GEMINI_SESSION_ID:-}" ]]; then
    HOST_PROVIDER="gemini"
  else
    HOST_PROVIDER=""
  fi
}

resolve_agents() {
  RUNTIME_CONFIG_FILE=$(mktemp)

  local resolution_output
  if ! resolution_output=$(python3 - "$CONFIG_FILE" "$AGENTS_SPEC" "$RUNTIME_CONFIG_FILE" "$HOST_PROVIDER" "$SKIP_PROVIDER" <<'PY'
import json
import math
import os
import sys

config_path = sys.argv[1]
agents_spec = sys.argv[2].strip()
out_path = sys.argv[3]
host_provider = sys.argv[4].strip().lower()
skip_provider = sys.argv[5].strip().lower()

BUILTIN_ALIASES = {
    "opus": {
        "name": "Opus",
        "provider": "claude",
        "command_template": ["claude", "-p", "--output-format", "json", "--model", "opus", "--effort", "{EFFORT}"],
        "reasoning": {"default": "high", "allowed": ["low", "medium", "high"]},
        "prompt_transport": "arg",
        "metering": {"mode": "required", "format": "claude_json"},
    },
    "sonnet": {
        "name": "Sonnet",
        "provider": "claude",
        "command_template": ["claude", "-p", "--output-format", "json", "--model", "sonnet", "--effort", "{EFFORT}"],
        "reasoning": {"default": "high", "allowed": ["low", "medium", "high"]},
        "prompt_transport": "arg",
        "metering": {"mode": "required", "format": "claude_json"},
    },
    "codex": {
        "name": "Codex",
        "provider": "codex",
        "command_template": ["codex", "exec", "--json", "--skip-git-repo-check", "-c", "model_reasoning_effort=\"{EFFORT}\""],
        "reasoning": {"default": "high", "allowed": ["low", "medium", "high"]},
        "prompt_transport": "arg",
        "metering": {
            "mode": "required",
            "format": "codex_jsonl",
            "pricing": {
                "input_per_million": 5.0,
                "cached_input_per_million": 0.5,
                "output_per_million": 30.0,
            },
        },
    },
    "gemini": {
        "name": "Gemini (Auto)",
        "provider": "gemini",
        "command_template": ["gemini", "-p"],
        "prompt_transport": "arg",
        "metering": {"mode": "none"},
    },
    "copilot": {
        "name": "Copilot",
        "provider": "copilot",
        "command_template": ["copilot", "-s", "--yolo", "--no-ask-user", "--model", "gpt-5-mini", "--output-format", "json", "-p"],
        "prompt_transport": "arg",
        "metering": {"mode": "none", "format": "copilot_json"},
    },
}

BUILTIN_DEBATE = {
    "default_agents": ["opus", "codex"],
    "min_agents": 2,
    "max_agents": 4,
}

def fail(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    raise SystemExit(1)

if config_path:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON in config ({config_path}): {e}")

    aliases = cfg.get("aliases")
    debate = cfg.get("debate")
    if not isinstance(aliases, dict) or not aliases:
        fail("config.aliases must be a non-empty object")
    if not isinstance(debate, dict):
        fail("config.debate must be an object")

    default_agents = debate.get("default_agents")
    min_agents = debate.get("min_agents", 2)
    max_agents = debate.get("max_agents", BUILTIN_DEBATE["max_agents"])
else:
    aliases = BUILTIN_ALIASES
    default_agents = BUILTIN_DEBATE["default_agents"]
    min_agents = BUILTIN_DEBATE["min_agents"]
    max_agents = BUILTIN_DEBATE["max_agents"]

if not isinstance(default_agents, list) or not default_agents:
    fail("debate.default_agents must be a non-empty array")
if not isinstance(min_agents, int) or not isinstance(max_agents, int) or min_agents > max_agents:
    fail("debate.min_agents/max_agents must be valid integers with min_agents <= max_agents")

if agents_spec:
    requested = [tok.strip() for tok in agents_spec.split(",") if tok.strip()]
else:
    requested = list(default_agents)

if len(requested) < min_agents or len(requested) > max_agents:
    fail(f"agent count must be between {min_agents} and {max_agents}, got {len(requested)}")

resolved = []
unique_participants = set()
provider_counts = {}

for token in requested:
    if ":" in token:
        alias_raw, model_override = token.split(":", 1)
        model_override = model_override.strip()
    else:
        alias_raw, model_override = token, ""

    alias = alias_raw.strip().lower()
    if alias not in aliases:
        available = ", ".join(sorted(aliases.keys()))
        fail(f"unknown alias '{alias}'. Available: {available}")

    spec = aliases[alias]
    if not isinstance(spec, dict):
        fail(f"alias '{alias}' must be an object")

    provider = spec.get("provider", "")
    if provider is None:
        provider = ""
    if not isinstance(provider, str):
        fail(f"alias '{alias}' provider must be a string when set")
    provider = provider.strip().lower()

    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        fail(f"alias '{alias}' has invalid name")

    template = spec.get("command_template")
    if not isinstance(template, list) or not template or not all(isinstance(x, str) and x for x in template):
        fail(f"alias '{alias}' command_template must be a non-empty array of strings")

    transport = spec.get("prompt_transport", "arg")
    if transport not in ("arg", "stdin"):
        fail(f"alias '{alias}' prompt_transport must be 'arg' or 'stdin'")

    metering = spec.get("metering")
    if not isinstance(metering, dict):
        fail(f"alias '{alias}' must declare metering")
    metering_mode = metering.get("mode")
    if metering_mode not in ("required", "none"):
        fail(f"alias '{alias}' metering.mode must be 'required' or 'none'")
    if metering_mode == "required":
        metering_format = metering.get("format")
        if metering_format not in ("claude_json", "codex_jsonl"):
            fail(f"alias '{alias}' has unsupported required metering format")
        if metering_format == "codex_jsonl":
            pricing = metering.get("pricing")
            pricing_keys = ("input_per_million", "cached_input_per_million", "output_per_million")
            if not isinstance(pricing, dict) or any(
                not isinstance(pricing.get(key), (int, float))
                or isinstance(pricing.get(key), bool)
                or not math.isfinite(float(pricing[key]))
                or pricing[key] < 0
                for key in pricing_keys
            ):
                fail(f"alias '{alias}' codex_jsonl metering requires pricing")

    has_model_placeholder = any("{MODEL}" in part for part in template)
    has_effort_placeholder = any("{EFFORT}" in part for part in template)

    model_value = model_override or spec.get("default_model", "")
    reasoning = spec.get("reasoning", {})
    if reasoning is None:
        reasoning = {}
    if not isinstance(reasoning, dict):
        fail(f"alias '{alias}' reasoning must be an object")
    effort_value = reasoning.get("default", "")
    if has_effort_placeholder and not effort_value:
        fail(f"alias '{alias}' requires reasoning.default when using {{EFFORT}}")
    allowed_efforts = reasoning.get("allowed")
    if effort_value and allowed_efforts is not None:
        if not isinstance(allowed_efforts, list) or not all(isinstance(v, str) and v for v in allowed_efforts):
            fail(f"alias '{alias}' reasoning.allowed must be an array of non-empty strings")
        if effort_value not in allowed_efforts:
            fail(f"alias '{alias}' reasoning.default '{effort_value}' is not in reasoning.allowed")

    cmd = []
    for part in template:
        if "{MODEL}" in part:
            if not model_value:
                fail(f"alias '{alias}' requires a model override or default_model")
            part = part.replace("{MODEL}", model_value)
        if "{EFFORT}" in part:
            if not effort_value:
                fail(f"alias '{alias}' requires reasoning.default when using {{EFFORT}}")
            part = part.replace("{EFFORT}", effort_value)
        cmd.append(part)

    # If model override is provided but template has no {MODEL}, inject safely.
    # Prefer replacing an existing --model value; otherwise insert before prompt
    # flag for arg-style CLIs so `-p/--prompt` still receives the prompt text.
    if model_override and not has_model_placeholder:
        replaced_model = False
        for i, token_part in enumerate(cmd):
            if token_part == "--model" and i + 1 < len(cmd):
                cmd[i + 1] = model_override
                replaced_model = True
                break

        if not replaced_model:
            insert_at = None
            if transport == "arg":
                for prompt_flag in ("--prompt", "-p"):
                    if prompt_flag in cmd:
                        # Insert before the last prompt flag occurrence.
                        rev_idx = cmd[::-1].index(prompt_flag)
                        insert_at = len(cmd) - 1 - rev_idx
                        break
            if insert_at is None:
                cmd.extend(["--model", model_override])
            else:
                cmd[insert_at:insert_at] = ["--model", model_override]

    if not provider and cmd:
        cmd_base = os.path.basename(cmd[0]).lower()
        if cmd_base in ("claude", "codex", "gemini", "copilot"):
            provider = cmd_base

    participant_key = (provider, tuple(cmd), transport)
    unique_participants.add(participant_key)

    resolved.append(
        {
            "name": name.strip(),
            "provider": provider,
            "command": cmd,
            "prompt_transport": transport,
            "metering": metering,
        }
    )
    provider_key = provider or "unknown"
    provider_counts[provider_key] = provider_counts.get(provider_key, 0) + 1

if len(unique_participants) < 2:
    fail("need at least two unique participants")


with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"agents": resolved}, f)

print(len(resolved))
for agent in resolved:
    print(f"{agent['name']}\t{agent.get('provider', '')}")
PY
  ); then
    exit 1
  fi

  local resolved_lines=()
  local line
  while IFS= read -r line; do
    resolved_lines+=("$line")
  done <<< "$resolution_output"

  if [[ ${#resolved_lines[@]} -lt 2 ]]; then
    echo "Error: failed to resolve agent config"
    exit 1
  fi

  AGENT_COUNT="${resolved_lines[0]}"
  AGENT_NAMES=()
  AGENT_PROVIDERS=()
  local i
  local resolved_name resolved_provider
  for (( i=1; i<${#resolved_lines[@]}; i++ )); do
    IFS=$'\t' read -r resolved_name resolved_provider <<< "${resolved_lines[$i]}"
    if [[ -z "$resolved_name" ]]; then
      echo "Error: resolved agent entry missing name"
      exit 1
    fi
    AGENT_NAMES+=("$resolved_name")
    AGENT_PROVIDERS+=("$resolved_provider")
  done

  if [[ "${#AGENT_NAMES[@]}" -ne "$AGENT_COUNT" ]]; then
    echo "Error: resolved agent count mismatch"
    exit 1
  fi
  if [[ "${#AGENT_PROVIDERS[@]}" -ne "$AGENT_COUNT" ]]; then
    echo "Error: resolved provider count mismatch"
    exit 1
  fi

  AGENT_1="${AGENT_NAMES[0]}"
  AGENT_2="${AGENT_NAMES[1]}"
}

restore_agents_from_state() {
  local debate_file="$1"
  local state_file_candidate="${debate_file%.md}.state.json"
  if [[ ! -f "$state_file_candidate" ]]; then
    return 1
  fi

  RUNTIME_CONFIG_FILE=$(mktemp)

  local restoration_output
  if ! restoration_output=$(python3 - "$state_file_candidate" "$debate_file" "$RUNTIME_CONFIG_FILE" <<'PY'
import json
import os
import sys

state_path = sys.argv[1]
debate_path = os.path.realpath(os.path.expanduser(sys.argv[2]))
out_path = sys.argv[3]

with open(state_path, "r", encoding="utf-8") as f:
    state = json.load(f)

state_debate = state.get("debate_file")
if isinstance(state_debate, str) and state_debate.strip():
    if os.path.realpath(os.path.expanduser(state_debate)) != debate_path:
        raise SystemExit(1)

saved_agents = state.get("resolved_agents")
if not isinstance(saved_agents, list) or len(saved_agents) < 2:
    raise SystemExit(1)

restored = []
for agent in saved_agents:
    if not isinstance(agent, dict):
        raise SystemExit(1)
    name = str(agent.get("name", "")).strip()
    provider = str(agent.get("provider", "")).strip().lower()
    command = agent.get("command", [])
    transport = str(agent.get("prompt_transport", "arg")).strip().lower()
    metering = agent.get("metering")
    if not name:
        raise SystemExit(1)
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise SystemExit(1)
    if transport not in ("arg", "stdin"):
        raise SystemExit(1)
    if not isinstance(metering, dict) or metering.get("mode") not in ("required", "none"):
        raise SystemExit(1)
    restored.append(
        {
            "name": name,
            "provider": provider,
            "command": command,
            "prompt_transport": transport,
            "metering": metering,
        }
    )

with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"agents": restored}, f)

print(len(restored))
for agent in restored:
    print(f"{agent['name']}\t{agent.get('provider', '')}")
PY
  ); then
    rm -f "$RUNTIME_CONFIG_FILE"
    RUNTIME_CONFIG_FILE=""
    return 1
  fi

  local restored_lines=()
  local line
  while IFS= read -r line; do
    restored_lines+=("$line")
  done <<< "$restoration_output"

  if [[ ${#restored_lines[@]} -lt 2 ]]; then
    rm -f "$RUNTIME_CONFIG_FILE"
    RUNTIME_CONFIG_FILE=""
    return 1
  fi

  AGENT_COUNT="${restored_lines[0]}"
  AGENT_NAMES=()
  AGENT_PROVIDERS=()
  local i restored_name restored_provider
  for (( i=1; i<${#restored_lines[@]}; i++ )); do
    IFS=$'\t' read -r restored_name restored_provider <<< "${restored_lines[$i]}"
    if [[ -z "$restored_name" ]]; then
      rm -f "$RUNTIME_CONFIG_FILE"
      RUNTIME_CONFIG_FILE=""
      return 1
    fi
    AGENT_NAMES+=("$restored_name")
    AGENT_PROVIDERS+=("$restored_provider")
  done

  if [[ "${#AGENT_NAMES[@]}" -ne "$AGENT_COUNT" ]]; then
    rm -f "$RUNTIME_CONFIG_FILE"
    RUNTIME_CONFIG_FILE=""
    return 1
  fi
  if [[ "${#AGENT_PROVIDERS[@]}" -ne "$AGENT_COUNT" ]]; then
    rm -f "$RUNTIME_CONFIG_FILE"
    RUNTIME_CONFIG_FILE=""
    return 1
  fi

  AGENT_1="${AGENT_NAMES[0]}"
  AGENT_2="${AGENT_NAMES[1]}"
  return 0
}

validate_host_provider_skip() {
  if [[ "$HOST_PROVIDER" != "claude" && "$HOST_PROVIDER" != "codex" && "$HOST_PROVIDER" != "gemini" && "$HOST_PROVIDER" != "copilot" ]]; then
    return
  fi

  local host_count=0
  local provider
  for provider in "${AGENT_PROVIDERS[@]}"; do
    if [[ "$provider" == "$HOST_PROVIDER" ]]; then
      host_count=$((host_count + 1))
    fi
  done

}

guardrails_support_agent_count() {
  local count="$1"
  grep -q "{OTHER_AGENTS}" "$GUARDRAILS" || return 1
  if (( count >= 3 )); then
    grep -q "\[A3-R1\]" "$GUARDRAILS" || return 1
  fi
  if (( count >= 4 )); then
    grep -q "\[A4-R1\]" "$GUARDRAILS" || return 1
  fi
  return 0
}

escape_for_sed() {
  printf '%s' "$1" | sed -e 's/[\/&|]/\\&/g'
}

has_open_disputes() {
  python3 "$SCRIPT_DIR/debate_lib.py" open-disputes "$DEBATE_FILE"
}

tag_for_turn() {
  local agent_idx="$1"
  local round="$2"
  printf '[A%s-R%s]' "$agent_idx" "$round"
}

count_tag_in_file() {
  local file="$1"
  local tag="$2"
  local count
  count=$(grep -oF "$tag" "$file" 2>/dev/null | wc -l || true)
  count=$(printf '%s' "$count" | tr -d '[:space:]')
  [[ -z "$count" ]] && count=0
  printf '%s' "$count"
}

count_tag_in_text() {
  local text="$1"
  local tag="$2"
  local count
  count=$(printf '%s' "$text" | grep -oF "$tag" 2>/dev/null | wc -l || true)
  count=$(printf '%s' "$count" | tr -d '[:space:]')
  [[ -z "$count" ]] && count=0
  printf '%s' "$count"
}

looks_like_debate_file() {
  local content="$1"
  [[ "$content" =~ ^[[:space:]]*#\ Debate: ]] \
    && [[ "$content" == *"## Proposal"* ]] \
    && [[ "$content" == *"## Dispute Log"* ]] \
    && [[ "$content" == *"| Round | Agent |"* ]]
}

all_agents_contributed_in_round() {
  local round="$1"
  local idx tag
  for (( idx=1; idx<=AGENT_COUNT; idx++ )); do
    tag=$(tag_for_turn "$idx" "$round")
    if [[ "$(count_tag_in_file "$DEBATE_FILE" "$tag")" -eq 0 ]]; then
      return 1
    fi
  done
  return 0
}

latest_substantive_change_reviewed() {
  local round="$1"
  python3 - "$STATE_FILE" "$round" <<'PY'
import json
import sys

state_file, round_s = sys.argv[1:3]
with open(state_file, "r", encoding="utf-8") as f:
    events = json.load(f).get("events", [])

turns = [
    event for event in events
    if event.get("type") == "turn"
    and event.get("round") == int(round_s)
    and event.get("status") in {"completed", "completed_substantive", "host_completed"}
]
last_substantive = next(
    (index for index in range(len(turns) - 1, -1, -1)
     if turns[index].get("status") == "completed_substantive"),
    None,
)
if last_substantive is None:
    raise SystemExit(0)

author = turns[last_substantive].get("agent_index")
reviewed = any(
    event.get("agent_index") != author
    for event in turns[last_substantive + 1:]
)
raise SystemExit(0 if reviewed else 1)
PY
}

is_converged() {
  local round="$1"
  python3 "$SCRIPT_DIR/debate_lib.py" proposal-converged "$DEBATE_FILE" \
    && ! has_open_disputes \
    && all_agents_contributed_in_round "$round" \
    && latest_substantive_change_reviewed "$round"
}

is_plan_converged() {
  python3 "$SCRIPT_DIR/debate_lib.py" plan-converged "$DEBATE_FILE" \
    && ! has_open_disputes
}

state_init() {
  python3 - "$STATE_FILE" "$DEBATE_FILE" "$HOST_PROVIDER" "$SKIP_PROVIDER" "$RUNTIME_CONFIG_FILE" "$AGENT_COUNT" "${AGENT_NAMES[@]}" -- "${AGENT_PROVIDERS[@]}" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

state_file = sys.argv[1]
debate_file = sys.argv[2]
host_provider = sys.argv[3]
skip_provider = sys.argv[4]
runtime_config_file = sys.argv[5]
agent_count = int(sys.argv[6])
args = sys.argv[7:]
sep = args.index("--")
agent_names = args[:sep]
agent_providers = args[sep + 1:]

planned_agents = []
for idx in range(agent_count):
    planned_agents.append(
        {
            "index": idx + 1,
            "name": agent_names[idx] if idx < len(agent_names) else f"Agent {idx + 1}",
            "provider": agent_providers[idx] if idx < len(agent_providers) else "",
        }
    )

state = {}
if os.path.exists(state_file):
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            existing = json.load(f)
        existing_debate = existing.get("debate_file")
        if (
            isinstance(existing, dict)
            and isinstance(existing_debate, str)
            and os.path.realpath(os.path.expanduser(existing_debate)) == os.path.realpath(os.path.expanduser(debate_file))
        ):
            state = existing
    except (OSError, json.JSONDecodeError):
        pass

state.update({
    "version": 2,
    "debate_file": debate_file,
    "created_at": state.get("created_at", datetime.now(timezone.utc).isoformat()),
    "host_provider": host_provider,
    "skip_provider": skip_provider,
    "status": "running",
    "planned_agents": planned_agents,
    "resolved_agents": [],
})
state.setdefault("events", [])
state.setdefault("attempts", [])

if runtime_config_file and os.path.exists(runtime_config_file):
    try:
        with open(runtime_config_file, "r", encoding="utf-8") as f:
            runtime_cfg = json.load(f)
        runtime_agents = runtime_cfg.get("agents", [])
        if isinstance(runtime_agents, list):
            normalized = []
            for agent in runtime_agents:
                if not isinstance(agent, dict):
                    continue
                name = str(agent.get("name", "")).strip()
                provider = str(agent.get("provider", "")).strip().lower()
                command = agent.get("command", [])
                transport = str(agent.get("prompt_transport", "arg")).strip().lower()
                metering = agent.get("metering")
                if (
                    name
                    and isinstance(command, list)
                    and command
                    and all(isinstance(x, str) and x for x in command)
                    and transport in ("arg", "stdin")
                    and isinstance(metering, dict)
                    and metering.get("mode") in ("required", "none")
                ):
                    normalized.append(
                        {
                            "name": name,
                            "provider": provider,
                            "command": command,
                            "prompt_transport": transport,
                            "metering": metering,
                        }
                    )
            state["resolved_agents"] = normalized
    except Exception:
        pass

with open(state_file, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)
PY
}

state_add_event() {
  local event_type="$1"
  local round="$2"
  local agent_idx="$3"
  local status="$4"
  local detail="$5"
  python3 - "$STATE_FILE" "$event_type" "$round" "$agent_idx" "$status" "$detail" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

state_file, event_type, round_s, agent_idx_s, status, detail = sys.argv[1:7]

if os.path.exists(state_file):
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
else:
    state = {"events": [], "status": "running"}

events = state.setdefault("events", [])
events.append(
    {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "round": int(round_s),
        "agent_index": int(agent_idx_s),
        "status": status,
        "detail": detail,
    }
)

with open(state_file, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)
PY
}

state_record_attempt() {
  local attempt_id="$1"
  local round="$2"
  local phase="$3"
  local agent_idx="$4"
  local status="$5"
  local metering_file="$6"
  python3 - "$SCRIPT_DIR" "$STATE_FILE" "$RUNTIME_CONFIG_FILE" "$attempt_id" "$round" "$phase" "$agent_idx" "$status" "$metering_file" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

script_dir, state_file, config_file, attempt_id, round_s, phase, agent_idx_s, status, metering_file = sys.argv[1:10]
sys.path.insert(0, script_dir)
from debate_accounting import record_attempt

with open(config_file, "r", encoding="utf-8") as f:
    agents = json.load(f).get("agents", [])
agent_idx = int(agent_idx_s)
agent = agents[agent_idx - 1]

metering = {"status": "failed", "usage": None, "cost_usd": None}
if os.path.exists(metering_file):
    try:
        with open(metering_file, "r", encoding="utf-8") as f:
            candidate = json.load(f)
        if isinstance(candidate, dict):
            metering = candidate
    except (OSError, json.JSONDecodeError):
        pass

record_attempt(
    state_file,
    {
        "attempt_id": attempt_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "round": int(round_s),
        "phase": phase,
        "agent_index": agent_idx,
        "agent": agent.get("name", f"Agent {agent_idx}"),
        "provider": agent.get("provider", ""),
        "status": status,
        "metering_status": metering.get("status", "failed"),
        "usage": metering.get("usage"),
        "cost_usd": metering.get("cost_usd"),
        **({"metering_error": metering["error"]} if metering.get("error") else {}),
    },
)
PY
}

next_attempt_id() {
  local round="$1"
  local agent_idx="$2"
  local phase="$3"
  python3 - "$STATE_FILE" "$round" "$agent_idx" "$phase" <<'PY'
import json
import os
import re
import sys

state_file, round_s, agent_idx_s, phase = sys.argv[1:5]
prefix = f"R{round_s}-A{agent_idx_s}-{phase}-"
max_sequence = 0
if os.path.exists(state_file):
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            attempts = json.load(f).get("attempts", [])
        for attempt in attempts:
            attempt_id = attempt.get("attempt_id", "") if isinstance(attempt, dict) else ""
            match = re.fullmatch(re.escape(prefix) + r"([0-9]+)", attempt_id)
            if match:
                max_sequence = max(max_sequence, int(match.group(1)))
    except (OSError, json.JSONDecodeError):
        pass
print(f"{prefix}{max_sequence + 1}")
PY
}

state_set_status() {
  local status="$1"
  local detail="$2"
  local round="$3"
  python3 - "$STATE_FILE" "$status" "$detail" "$round" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

state_file, status, detail, round_s = sys.argv[1:5]
if os.path.exists(state_file):
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
else:
    state = {"events": []}

state["status"] = status
state["updated_at"] = datetime.now(timezone.utc).isoformat()
state["last_detail"] = detail
state["current_round"] = int(round_s)

with open(state_file, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)
PY
}

host_turn_completed() {
  local agent_idx="$1"
  local round="$2"
  python3 - "$STATE_FILE" "$agent_idx" "$round" <<'PY'
import json
import sys

state_file, agent_idx, round_s = sys.argv[1:4]
with open(state_file, "r", encoding="utf-8") as f:
    events = json.load(f).get("events", [])
complete = any(
    event.get("type") == "turn"
    and event.get("agent_index") == int(agent_idx)
    and event.get("round") == int(round_s)
    and event.get("status") in {"host_completed", "completed_substantive"}
    for event in events
)
raise SystemExit(0 if complete else 1)
PY
}

pending_host_turns() {
  python3 - "$STATE_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    events = json.load(f).get("events", [])
latest = {}
for event in events:
    if event.get("type") == "turn":
        latest[(event.get("agent_index"), event.get("round"))] = event.get("status")
for (agent_idx, round_s), status in latest.items():
    if status == "pending_host":
        print(agent_idx, round_s)
PY
}

ensure_host_turn_present() {
  local agent_idx="$1"
  local round="$2"
  local tag snapshot_file
  tag=$(tag_for_turn "$agent_idx" "$round")
  snapshot_file="${STATE_FILE%.state.json}.host-A${agent_idx}-R${round}.before"
  if host_turn_completed "$agent_idx" "$round"; then
    return 0
  fi
  if [[ "$(count_tag_in_file "$DEBATE_FILE" "$tag")" -eq 0 ]]; then
    cp "$DEBATE_FILE" "$snapshot_file"
    echo "  Host action required: missing $tag for ${AGENT_NAMES[$((agent_idx - 1))]} in Round $round."
    echo "  Add your host-provider edit in the debate file, then rerun with --resume \"$DEBATE_FILE\"."
    state_add_event "turn" "$round" "$agent_idx" "pending_host" "Missing required host tag $tag"
    return 1
  fi

  local turn_status="completed_substantive"
  if [[ -f "$snapshot_file" ]]; then
    local preservation_report
    if ! preservation_report=$(python3 "$SCRIPT_DIR/debate_lib.py" verify-preservation "$snapshot_file" "$DEBATE_FILE"); then
      echo "  Error: host turn rewrote or dropped earlier debate content:"
      printf '    %s\n' "$preservation_report"
      state_add_event "turn" "$round" "$agent_idx" "failed" "Host dropped earlier content: $preservation_report"
      return 1
    fi
    if ! python3 "$SCRIPT_DIR/debate_lib.py" substantive-change "$snapshot_file" "$DEBATE_FILE"; then
      turn_status="host_completed"
    fi
    rm -f "$snapshot_file"
  fi
  state_add_event "turn" "$round" "$agent_idx" "$turn_status" "Found host tag $tag"
  return 0
}

verify_round_participation() {
  local round="$1"
  local idx tag
  local missing=""
  for (( idx=1; idx<=AGENT_COUNT; idx++ )); do
    tag=$(tag_for_turn "$idx" "$round")
    if [[ "$(count_tag_in_file "$DEBATE_FILE" "$tag")" -eq 0 ]]; then
      if [[ -z "$missing" ]]; then
        missing="$tag"
      else
        missing="$missing, $tag"
      fi
    fi
  done
  if [[ -n "$missing" ]]; then
    echo "  Error: round $round is incomplete. Missing tags: $missing"
    state_set_status "failed" "Round $round incomplete: missing $missing" "$round"
    return 1
  fi
  return 0
}

agent_list_text() {
  local joined="${AGENT_NAMES[0]}"
  local idx
  for (( idx=1; idx<AGENT_COUNT; idx++ )); do
    joined="${joined}, ${AGENT_NAMES[$idx]}"
  done
  printf '%s' "$joined"
}

detect_resume_round() {
  local debate_file="$1"
  local state_file_candidate="${debate_file%.md}.state.json"
  AGENT_COUNT="$AGENT_COUNT" python3 - "$debate_file" "$state_file_candidate" "$SCRIPT_DIR" <<'PY'
import json
import os
import re
import sys

sys.path.insert(0, sys.argv[3])
from debate_lib import collect_turn_tags

debate_path = os.path.realpath(os.path.expanduser(sys.argv[1]))
state_path = sys.argv[2]


def detect_from_state():
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return None

    state_debate = state.get("debate_file")
    if isinstance(state_debate, str) and state_debate.strip():
        try:
            if os.path.realpath(os.path.expanduser(state_debate)) != debate_path:
                return None
        except Exception:
            return None

    current_round = state.get("current_round")
    if not isinstance(current_round, int) or current_round < 1:
        return None

    status = str(state.get("status", "")).strip().lower()
    # A killed or stopped run leaves current_round stale; the tags are the truth then.
    if status in {"running", "stopped"}:
        return None
    terminal_statuses = {
        "converged",
        "max_rounds_reached",
        "plan_converged",
        "plan_max_rounds",
    }
    if status in terminal_statuses:
        return current_round + 1
    return current_round


def detect_from_tags():
    try:
        with open(debate_path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return 1

    tags = collect_turn_tags(text)
    agent_count = int(os.environ.get("AGENT_COUNT", "0") or 0)
    max_round = 0
    rounds_with_agent = {}
    for tag in tags:
        # tag looks like "[A<n>-R<r>]"
        m_tag = re.match(r"^\[A([0-9]+)-R([0-9]+)\]$", tag)
        if not m_tag:
            continue
        agent_num = int(m_tag.group(1))
        round_num = int(m_tag.group(2))
        if agent_count and not (1 <= agent_num <= agent_count):
            continue
        max_round = max(max_round, round_num)
        rounds_with_agent.setdefault(round_num, set()).add(agent_num)

    if max_round == 0:
        return 1

    agents_present = rounds_with_agent.get(max_round, set())
    if agent_count and agents_present >= set(range(1, agent_count + 1)):
        return max_round + 1
    return max_round


resolved = detect_from_state()
if resolved is None:
    resolved = detect_from_tags()
if resolved is None:
    resolved = 1

print(max(1, int(resolved)))
PY
}

resolve_config
detect_host_provider
if [[ -n "$RESUME_FILE" && -z "$AGENTS_SPEC" ]]; then
  if restore_agents_from_state "$RESUME_FILE"; then
    echo "Restoring agent lineup from state file for resume."
  else
    resolve_agents
  fi
else
  resolve_agents
fi
validate_host_provider_skip

if [[ -n "$AGENT1_OVERRIDE_NAME" ]]; then
  AGENT_NAMES[0]="$AGENT1_OVERRIDE_NAME"
fi
if [[ -n "$AGENT2_OVERRIDE_NAME" ]]; then
  AGENT_NAMES[1]="$AGENT2_OVERRIDE_NAME"
fi
if [[ -n "$AGENT3_OVERRIDE_NAME" && "$AGENT_COUNT" -ge 3 ]]; then
  AGENT_NAMES[2]="$AGENT3_OVERRIDE_NAME"
fi
if [[ -n "$AGENT4_OVERRIDE_NAME" && "$AGENT_COUNT" -ge 4 ]]; then
  AGENT_NAMES[3]="$AGENT4_OVERRIDE_NAME"
fi

AGENT_1="${AGENT_NAMES[0]}"
AGENT_2="${AGENT_NAMES[1]}"

if (( AGENT_COUNT >= 3 )) && ! guardrails_support_agent_count "$AGENT_COUNT"; then
  echo "Error: guardrails file does not support ${AGENT_COUNT}-agent debates"
  exit 1
fi

# --- Create or resume debate file ---

if [[ -n "$RESUME_FILE" ]]; then
  DEBATE_FILE="$RESUME_FILE"
  if [[ ! -f "$DEBATE_FILE" ]]; then
    echo "Error: debate file not found: $DEBATE_FILE"
    exit 1
  fi
  echo "Resuming debate: $DEBATE_FILE"
  # Detect round from state (preferred) or existing turn tags.
  CURRENT_ROUND=$(detect_resume_round "$DEBATE_FILE")
  resume_present_names=""
  resume_pending_names=""
  for (( ri=1; ri<=AGENT_COUNT; ri++ )); do
    resume_tag=$(tag_for_turn "$ri" "$CURRENT_ROUND")
    if [[ "$(count_tag_in_file "$DEBATE_FILE" "$resume_tag")" -gt 0 ]]; then
      resume_present_names="${resume_present_names}${resume_present_names:+, }${AGENT_NAMES[$((ri - 1))]}"
    else
      resume_pending_names="${resume_pending_names}${resume_pending_names:+, }${AGENT_NAMES[$((ri - 1))]}"
    fi
  done
  echo "Resuming at Round $CURRENT_ROUND. Already present: ${resume_present_names:-none}. Will run: ${resume_pending_names:-none}."
else
  mkdir -p "$DEBATES_DIR"
  # Auto-increment debate number from existing files (safe when directory is empty)
  LAST_NUM=0
  shopt -s nullglob
  for path in "$DEBATES_DIR"/*.md; do
    base="${path##*/}"
    if [[ "$base" =~ ^([0-9]+)- ]]; then
      num="${BASH_REMATCH[1]}"
      if (( num > LAST_NUM )); then
        LAST_NUM="$num"
      fi
    fi
  done
  shopt -u nullglob
  NEXT_NUM=$(( LAST_NUM + 1 ))
  SLUG=$(echo "$TOPIC" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-' | head -c 40)
  DEBATE_FILE="$DEBATES_DIR/${NEXT_NUM}-$(date +%Y-%m-%d)-${SLUG}.md"
  CURRENT_ROUND=1

  # Build newline-separated file list, warning on missing paths
  FILES_LINES=""
  if [[ -n "$FILES" ]]; then
    for f in $FILES; do
      if [[ -f "$f" ]]; then
        FILES_LINES="${FILES_LINES}${f}
"
      else
        echo "Warning: file not found, skipping: $f"
      fi
    done
  fi

  # Render from template in python: values are substituted verbatim, so
  # topics/constraints containing sed metacharacters cannot corrupt the file.
  if ! DL_TOPIC="$TOPIC" \
    DL_DATE="$(date +%Y-%m-%d)" \
    DL_MAX_ROUNDS="$MAX_ROUNDS" \
    DL_CONSTRAINTS="$CONSTRAINTS" \
    DL_AGENT_COUNT="$AGENT_COUNT" \
    DL_AGENT1="${AGENT_NAMES[0]}" \
    DL_AGENT2="${AGENT_NAMES[1]}" \
    DL_AGENT3="${AGENT_NAMES[2]:-}" \
    DL_AGENT4="${AGENT_NAMES[3]:-}" \
    DL_FILES="$FILES_LINES" \
    python3 "$SCRIPT_DIR/debate_lib.py" render "$TEMPLATE" "$DEBATE_FILE"; then
    echo "Error: failed to render debate file from template"
    exit 1
  fi

  echo "Created debate file: $DEBATE_FILE"
fi

STATE_FILE="${DEBATE_FILE%.md}.state.json"
state_init

if [[ -n "$RESUME_FILE" ]]; then
  while read -r pending_agent pending_round; do
    [[ -z "$pending_agent" ]] && continue
    if ! ensure_host_turn_present "$pending_agent" "$pending_round"; then
      state_set_status "paused_host_turn" "Host turn A${pending_agent}-R${pending_round} is not valid yet" "$pending_round"
      exit 2
    fi
  done < <(pending_host_turns)
fi

# --- Read guardrails ---
GUARDRAILS_TEXT=$(cat "$GUARDRAILS")

# --- Debate loop ---

invoke_agent() {
  local agent_idx="$1"
  local round="$2"
  local phase="${3:-debate}"
  local agent_name="${AGENT_NAMES[$((agent_idx - 1))]}"
  local agent_provider="${AGENT_PROVIDERS[$((agent_idx - 1))]}"
  local other_names=""
  local idx

  for (( idx=1; idx<=AGENT_COUNT; idx++ )); do
    if [[ "$idx" -eq "$agent_idx" ]]; then
      continue
    fi
    if [[ -z "$other_names" ]]; then
      other_names="${AGENT_NAMES[$((idx - 1))]}"
    else
      other_names="${other_names}, ${AGENT_NAMES[$((idx - 1))]}"
    fi
  done

  local debate_content
  debate_content=$(cat "$DEBATE_FILE")
  local required_tag
  required_tag=$(tag_for_turn "$agent_idx" "$round")
  local before_tag_count
  before_tag_count=$(count_tag_in_text "$debate_content" "$required_tag")

  # Build prompt: guardrails (with substitutions) + debate file
  local prompt
  local agent_name_esc other_names_esc
  agent_name_esc=$(escape_for_sed "$agent_name")
  other_names_esc=$(escape_for_sed "$other_names")
  prompt=$(echo "$GUARDRAILS_TEXT" | \
    sed -e "s|{AGENT_NAME}|$agent_name_esc|g" \
        -e "s|{OTHER_AGENT}|$other_names_esc|g" \
        -e "s|{OTHER_AGENTS}|$other_names_esc|g" \
        -e "s|{ROUND}|$round|g" \
        -e "s|{MAX_ROUNDS}|$MAX_ROUNDS|g")

  # Add plan-phase instructions if in plan mode
  local phase_instruction=""
  if [[ "$phase" == "plan" ]]; then
    if [[ "$agent_idx" -eq 1 ]]; then
      phase_instruction="

---

PLAN PHASE: The debate has converged. Write a concrete plan in the Plan section. Change PLAN_STATUS from PENDING to OPEN. Include: exact files to change, what to change (with line references), order of operations, and code snippets for non-trivial changes. Tag your edits as [$required_tag]."
    else
      phase_instruction="

---

PLAN PHASE: The debate has converged. Review the Plan section written by Agent 1. Apply the same guardrails: strikethrough to disagree, evidence required. Check that the plan implements what the debate agreed on. If the plan is correct and complete, mark PLAN_STATUS: CONVERGED. Tag your edits as [$required_tag]."
    fi
  fi

  local provider_instruction=""
  if [[ "$agent_provider" == "copilot" ]]; then
    provider_instruction="

---

COPILOT OUTPUT CONTRACT (STRICT):
- Your FIRST output character must be '#' from the line '# Debate:'.
- Do not include any lead-in text (for example: explanations, plans, or \"let me\" statements).
- Do not wrap the document in code fences.
- Return only the full updated debate file content."
  fi

  prompt="$prompt
$phase_instruction
$provider_instruction
---

Below is the current state of the debate file. Edit it according to the guardrails above.

CRITICAL: Return ONLY the complete updated debate file. No preamble, no summary, no commentary, no code fences. Your entire response must be the full markdown file starting with the heading and ending with the Dispute Log table. Any text outside the debate file content will cause a parsing failure.

---

$debate_content"

  local prompt_file
  prompt_file=$(mktemp)
  printf '%s' "$prompt" > "$prompt_file"

  echo "  Invoking $agent_name (Round $round)..."
  local response metering_file attempt_id
  metering_file=$(mktemp)
  attempt_id=$(next_attempt_id "$round" "$agent_idx" "$phase")
  local response_out provider_rc=0
  response_out=$(mktemp)
  # Background + wait so a stop signal is handled now and on_stop can kill the provider tree.
  AGENT_DEBATE_TURN_LOG="${DEBATE_FILE%.md}.turns/${attempt_id}" python3 "$SCRIPT_DIR/provider_runner.py" "$RUNTIME_CONFIG_FILE" "$agent_idx" "$prompt_file" "$metering_file" > "$response_out" &
  PROVIDER_PID=$!
  wait "$PROVIDER_PID" || provider_rc=$?
  PROVIDER_PID=""
  response=$(cat "$response_out")
  rm -f "$response_out"
  if [[ "$provider_rc" -ne 0 ]]; then
    rm -f "$prompt_file"
    echo "  Error: $agent_name failed Round $round."
    state_record_attempt "$attempt_id" "$round" "$phase" "$agent_idx" "provider_failed" "$metering_file"
    rm -f "$metering_file"
    state_add_event "turn" "$round" "$agent_idx" "failed" "Agent command failed"
    state_set_status "failed" "$agent_name failed in round $round" "$round"
    return 1
  fi

  rm -f "$prompt_file"
  state_record_attempt "$attempt_id" "$round" "$phase" "$agent_idx" "provider_completed" "$metering_file"

  response="$(printf '%s' "$response" | python3 "$SCRIPT_DIR/agent_response_parser.py")"

  # Write response back to debate file
  if [[ -n "$response" ]]; then
    if ! looks_like_debate_file "$response"; then
      local raw_output_file
      raw_output_file="${DEBATE_FILE%.md}.agent${agent_idx}.round${round}.raw.txt"
      printf '%s' "$response" > "$raw_output_file"
      echo "  Error: $agent_name returned non-debate output in Round $round."
      if [[ "$response" == *"can't run nested Claude Code sessions"* ]]; then
        echo "  Hint: in host sessions, use --skip-provider <host> and have the host take its own turns directly."
      fi
      echo "  Raw output saved: $raw_output_file"
      state_record_attempt "$attempt_id" "$round" "$phase" "$agent_idx" "rejected" "$metering_file"
      rm -f "$metering_file"
      state_add_event "turn" "$round" "$agent_idx" "failed" "Non-debate output"
      state_set_status "failed" "$agent_name returned non-debate output in round $round" "$round"
      return 1
    fi

    if [[ "$response" == "$debate_content" ]]; then
      echo "  Error: $agent_name made no file changes in Round $round."
      state_record_attempt "$attempt_id" "$round" "$phase" "$agent_idx" "rejected" "$metering_file"
      rm -f "$metering_file"
      state_add_event "turn" "$round" "$agent_idx" "failed" "No content changes"
      state_set_status "failed" "$agent_name made no changes in round $round" "$round"
      return 1
    fi

    local after_tag_count
    after_tag_count=$(count_tag_in_text "$response" "$required_tag")
    if (( after_tag_count <= before_tag_count )); then
      echo "  Error: $agent_name response missing required turn tag $required_tag."
      state_record_attempt "$attempt_id" "$round" "$phase" "$agent_idx" "rejected" "$metering_file"
      rm -f "$metering_file"
      state_add_event "turn" "$round" "$agent_idx" "failed" "Missing required tag $required_tag"
      state_set_status "failed" "$agent_name missing required tag $required_tag in round $round" "$round"
      return 1
    fi

    # Reject responses that silently drop earlier contributions: every tag
    # already in the file must survive, and the dispute log must not shrink.
    local response_file preservation_report
    response_file=$(mktemp)
    printf '%s\n' "$response" > "$response_file"
    if ! preservation_report=$(python3 "$SCRIPT_DIR/debate_lib.py" verify-preservation "$DEBATE_FILE" "$response_file"); then
      local raw_output_file
      raw_output_file="${DEBATE_FILE%.md}.agent${agent_idx}.round${round}.raw.txt"
      mv "$response_file" "$raw_output_file"
      echo "  Error: $agent_name response dropped earlier debate content in Round $round:"
      printf '    %s\n' "$preservation_report"
      echo "  Rejected output saved: $raw_output_file"
      state_record_attempt "$attempt_id" "$round" "$phase" "$agent_idx" "rejected" "$metering_file"
      rm -f "$metering_file"
      state_add_event "turn" "$round" "$agent_idx" "failed" "Dropped earlier content: $preservation_report"
      state_set_status "failed" "$agent_name dropped earlier content in round $round" "$round"
      return 1
    fi

    local turn_status="completed"
    if python3 "$SCRIPT_DIR/debate_lib.py" substantive-change "$DEBATE_FILE" "$response_file"; then
      turn_status="completed_substantive"
    fi
    mv "$response_file" "$DEBATE_FILE"
    state_record_attempt "$attempt_id" "$round" "$phase" "$agent_idx" "accepted" "$metering_file"
    rm -f "$metering_file"
    echo "  $agent_name finished Round $round."
    state_add_event "turn" "$round" "$agent_idx" "$turn_status" "Applied changes with tag $required_tag"
  else
    echo "  Warning: $agent_name returned empty response in Round $round."
    state_record_attempt "$attempt_id" "$round" "$phase" "$agent_idx" "rejected" "$metering_file"
    rm -f "$metering_file"
    state_add_event "turn" "$round" "$agent_idx" "failed" "Empty response"
    state_set_status "failed" "$agent_name returned empty response in round $round" "$round"
    return 1
  fi
}

echo ""
echo "=== Agent Debate: $TOPIC ==="
echo "  Rounds: $MAX_ROUNDS | Agents: $(agent_list_text)"
if [[ -n "$CONFIG_FILE" ]]; then
  echo "  Config: $CONFIG_FILE"
fi
if [[ -n "$SKIP_PROVIDER" ]]; then
  skip_names=""
  skip_count=0
  for (( si=1; si<=AGENT_COUNT; si++ )); do
    if [[ "${AGENT_PROVIDERS[$((si - 1))]}" == "$SKIP_PROVIDER" ]]; then
      skip_names="${skip_names}${skip_names:+, }${AGENT_NAMES[$((si - 1))]}"
      skip_count=$((skip_count + 1))
    fi
  done
  if [[ "$skip_count" -gt 1 ]]; then
    echo "  Warning: --skip-provider $SKIP_PROVIDER skips $skip_count agents that share this provider: $skip_names"
  else
    echo "  Skip provider: $SKIP_PROVIDER ($skip_names, host-direct turns expected)"
  fi
fi
if [[ "$PLAN_MODE" == true ]]; then
  echo "  Plan phase: enabled (rounds: $PLAN_ROUNDS)"
else
  echo "  Plan phase: disabled (--no-plan)"
fi
echo ""

END_ROUND=$(( CURRENT_ROUND + MAX_ROUNDS - 1 ))

ACTIVE_AGENT_COUNT=0
for (( idx=1; idx<=AGENT_COUNT; idx++ )); do
  provider="${AGENT_PROVIDERS[$((idx - 1))]}"
  if [[ -n "$SKIP_PROVIDER" && "$provider" == "$SKIP_PROVIDER" ]]; then
    continue
  fi
  ACTIVE_AGENT_COUNT=$((ACTIVE_AGENT_COUNT + 1))
done
if [[ "$ACTIVE_AGENT_COUNT" -eq 0 ]]; then
  echo "Error: no runnable agents after applying --skip-provider $SKIP_PROVIDER"
  state_set_status "failed" "No runnable agents after skip-provider filter" "$CURRENT_ROUND"
  exit 1
fi

converged=false
last_completed_round=0
if [[ -n "$RESUME_FILE" && "$PLAN_MODE" == true ]]; then
  resume_last_round=$((CURRENT_ROUND - 1))
  if (( resume_last_round >= 1 )) && is_converged "$resume_last_round"; then
    converged=true
    last_completed_round="$resume_last_round"
    echo "Debate already converged at Round $resume_last_round (resume mode)."
    echo "Skipping additional debate rounds and entering plan phase."
  fi
fi

if [[ "$converged" != true ]]; then
  for (( round=CURRENT_ROUND; round<=END_ROUND; round++ )); do
    echo "--- Round $round ---"
    state_add_event "round_start" "$round" 0 "started" "Round $round started"
    round_summary="R${round}:"

    for (( idx=1; idx<=AGENT_COUNT; idx++ )); do
      provider="${AGENT_PROVIDERS[$((idx - 1))]}"
      # Host turns go first: ensure_host_turn_present validates and classifies an existing host turn.
      if [[ -n "$SKIP_PROVIDER" && "$provider" == "$SKIP_PROVIDER" ]]; then
        echo "  Host-direct turn for ${AGENT_NAMES[$((idx - 1))]} (provider: $provider)."
        if ! ensure_host_turn_present "$idx" "$round"; then
          round_summary="$round_summary A${idx}⏸"
          echo "  Round paused for host action."
          echo "  $round_summary"
          state_set_status "paused_host_turn" "Missing host turn A${idx}-R${round}" "$round"
          echo ""
          echo "=== Debate paused ==="
          echo "  Output: $DEBATE_FILE"
          echo "  State:  $STATE_FILE"
          echo "  Add host turn and rerun with --resume \"$DEBATE_FILE\""
          exit 2
        fi
        round_summary="$round_summary A${idx}✅"
        continue
      fi
      if [[ -n "$RESUME_FILE" && "$round" -eq "$CURRENT_ROUND" ]] \
        && [[ "$(count_tag_in_file "$DEBATE_FILE" "$(tag_for_turn "$idx" "$round")")" -gt 0 ]]; then
        echo "  ${AGENT_NAMES[$((idx - 1))]} already has $(tag_for_turn "$idx" "$round"); skipping."
        state_add_event "turn" "$round" "$idx" "preexisting" "Found existing tag on resume"
        round_summary="$round_summary A${idx}⏭"
        continue
      fi
      if invoke_agent "$idx" "$round"; then
        round_summary="$round_summary A${idx}✅"
      else
        round_summary="$round_summary A${idx}❌"
        echo "  $round_summary"
        echo ""
        echo "=== Debate failed ==="
        echo "  Output: $DEBATE_FILE"
        echo "  State:  $STATE_FILE"
        exit 1
      fi
    done

    if ! verify_round_participation "$round"; then
      echo "  $round_summary"
      echo ""
      echo "=== Debate failed ==="
      echo "  Output: $DEBATE_FILE"
      echo "  State:  $STATE_FILE"
      exit 1
    fi

    last_completed_round="$round"
    if is_converged "$round"; then
      # All agents see the same file — if STATUS is still CONVERGED after the last agent, they agree
      echo ""
      echo "=== CONVERGED at Round $round ==="
      echo "  $round_summary"
      state_set_status "converged" "Converged at round $round" "$round"
      converged=true
      break
    fi

    echo "  $round_summary"
    state_add_event "round_end" "$round" 0 "completed" "$round_summary"
    echo ""
  done
fi

if [[ "$converged" != true ]]; then
  state_set_status "max_rounds_reached" "Reached max rounds without convergence" "$END_ROUND"
fi

# --- Plan phase ---
if [[ "$PLAN_MODE" == true && "$converged" == true ]]; then
  echo ""
  echo "=== CONVERGED — entering Plan phase ==="
  state_add_event "plan_phase_start" "$last_completed_round" 0 "started" "Entering plan phase"

  plan_converged=false
  plan_start_round=$((last_completed_round + 1))
  plan_end_round=$((plan_start_round + PLAN_ROUNDS - 1))

  for (( plan_round=plan_start_round; plan_round<=plan_end_round; plan_round++ )); do
    echo "--- Plan Round $plan_round ---"
    state_add_event "plan_round_start" "$plan_round" 0 "started" "Plan round $plan_round started"
    plan_summary="P${plan_round}:"

    for (( idx=1; idx<=AGENT_COUNT; idx++ )); do
      provider="${AGENT_PROVIDERS[$((idx - 1))]}"
      if [[ -n "$SKIP_PROVIDER" && "$provider" == "$SKIP_PROVIDER" ]]; then
        echo "  Host-direct plan turn for ${AGENT_NAMES[$((idx - 1))]} (provider: $provider)."
        if ! ensure_host_turn_present "$idx" "$plan_round"; then
          plan_summary="$plan_summary A${idx}⏸"
          echo "  Plan round paused for host action."
          echo "  $plan_summary"
          state_set_status "paused_host_plan_turn" "Missing host plan turn A${idx}-R${plan_round}" "$plan_round"
          echo ""
          echo "=== Plan phase paused ==="
          echo "  Output: $DEBATE_FILE"
          echo "  State:  $STATE_FILE"
          echo "  Add host plan turn and rerun with --resume \"$DEBATE_FILE\""
          exit 2
        fi
        plan_summary="$plan_summary A${idx}✅"
        continue
      fi
      if invoke_agent "$idx" "$plan_round" "plan"; then
        plan_summary="$plan_summary A${idx}✅"
      else
        plan_summary="$plan_summary A${idx}❌"
        echo "  $plan_summary"
        echo ""
        echo "=== Plan phase failed ==="
        echo "  Output: $DEBATE_FILE"
        echo "  State:  $STATE_FILE"
        exit 1
      fi
    done

    if is_plan_converged; then
      echo ""
      echo "=== Plan CONVERGED at Round $plan_round ==="
      echo "  $plan_summary"
      state_set_status "plan_converged" "Plan converged at round $plan_round" "$plan_round"
      plan_converged=true
      break
    fi

    echo "  $plan_summary"
    state_add_event "plan_round_end" "$plan_round" 0 "completed" "$plan_summary"
    echo ""
  done

  if [[ "$plan_converged" != true ]]; then
    state_set_status "plan_max_rounds" "Plan phase reached max rounds without convergence" "$plan_end_round"
  fi
fi

echo ""
echo "=== Debate complete ==="
echo "  Output: $DEBATE_FILE"
echo "  State:  $STATE_FILE"
echo "  Review the file and make your call."
