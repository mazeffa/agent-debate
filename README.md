# agent-debate

<p align="center">
  <img src="assets/banner.png" alt="agent-debate banner" width="700">
</p>

AI agents debate your technical decisions — then you make the call.

Two to four agents (Claude, Codex, Gemini, Copilot) edit a shared markdown file in-place. They strikethrough to disagree, cite `file:line` as evidence, track disputes in a log, and must converge or escalate. It's adversarial code review, not a chatbot.

## What a debate looks like

Here's a real excerpt from a [3-agent debate on adding OpenRouter support](debates/1-2026-03-07-add-openrouter-support.md) (Claude vs Codex vs Gemini):

```markdown
~~Why a wrapper: Dependencies are just `curl` + `jq`, both standard
on macOS/Linux. [A1-R1]~~
Wrapper is correct, but `jq` is unnecessary dependency surface for v1.
Evidence: repo currently has no `jq` dependency, while `python3` is already
required by orchestrator (`orchestrate.sh:139,445,492,753`). Minimum viable
should be a Python wrapper using stdlib `json` + `urllib.request`. [A2-R1]

### Claude accepts Codex's corrections [A1-R2]

**Python wrapper over bash+jq:** Codex is right. Verified: `orchestrate.sh`
already requires `python3` at 4+ callsites. Adding `jq` as a new dependency
when Python stdlib can do the same job is unnecessary. Conceding. [A1-R2]
```

Agents propose, disagree with evidence, and concede when wrong. Every claim is grounded in actual code. The [full debate](debates/1-2026-03-07-add-openrouter-support.md) converged in 1 round with all disputes closed.

## Why not just ask one AI?

- **One agent has blind spots.** A second agent catches what the first missed — wrong assumptions, unnecessary dependencies, missing code paths.
- **Evidence, not vibes.** The protocol forces agents to cite `file:line`, paste log output, and verify each other's claims before agreeing.
- **Scope creep dies here.** Agents must justify every addition. "Easy to add" is not a reason. Unrelated ideas go to a parking lot.
- **You decide, they inform.** Agents converge on a recommendation. You pick what ships.

*10 debates. 12 points to Codex. 9 concessions from Claude. Gemini turned accepted fixes into production-ready plans.*

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/gumbel-ai/agent-debate/main/install.sh | bash
```

Claude Code gets an `agent-debate` skill at `~/.claude/skills/agent-debate/` (loaded on demand; nothing is added to `CLAUDE.md`). Codex, Gemini, and Copilot get an instruction block in their global `AGENTS.md`/`GEMINI.md`.

- **Claude** (`fable`, `opus`, `sonnet`) runs through [Claude Code](https://docs.anthropic.com/en/docs/claude-code) using your existing login. No API key.

Codex, Gemini CLI, and Copilot CLI aliases also exist if you have those tools installed.

Windows: run from Git Bash. The scripts handle Python's CRLF output and `.cmd` shims.

## Usage

Just tell any agent what you want:

```
"Start a debate on whether to use WebSockets or polling. Add Codex too."
```

```
"Continue debate 3 — I disagree with Codex's approach, argue for the simpler solution."
```

```
"Auto debate this auth refactor with Codex and Gemini, max 3 rounds."
```

Three modes:
- **Manual** — you switch between agent terminals, each takes a turn editing the shared file
- **Auto** — orchestrator runs agents round-robin until they converge or hit max rounds. By default it then runs a Plan phase; pass `--no-plan` to skip. Use `--skip-provider` if you want to participate as one of the agents yourself
  Plan controls: `--no-plan` (disable) and `--plan-rounds N` (default: 2). The Plan phase is Agent 1 writing and Agent 2 reviewing; if the converged Proposal already lists files, edits and checks, use `--no-plan`.
- **Pause between rounds** — create `<debate-file-stem>.pause`; the orchestrator deletes it at the next round boundary (or right after convergence, before the Plan phase), stops with exit 0, and `--resume` continues. Use it to add a host note.
- **Seeded auto** — you write the opening `[A1-R1]` proposal by hand, then `orchestrate.sh --resume <file> --agents ...` drives the rest. Agents missing from the file's highest round take their turns first.

## Loop Review

One agent codes, the other model reviews it in the background, and the coder cannot claim "done" while review feedback sits unread. Works both ways: Claude Code codes while Codex reviews, or the reverse. Say "Loop Review on" to start, "Loop Review off" to stop — from any project, the system installs globally.

See [How Loop Review Works](docs/how-loop-review-works.md) for the 2-minute explanation, and the [Loop Review reference](docs/loop-review.md) for commands, timing, runtime files, and configuration.

## How it works

All agents follow the same [guardrails](agent-guardrails.md):

| Rule | What it means |
|------|---------------|
| **Living document** | Agents edit in-place with ~~strikethrough~~ + counter, not append-only chat |
| **Evidence required** | Every claim must cite file:line, log data, or runtime output inline |
| **Disputes tracked** | Tabular log with OPEN/CLOSED/PARKED statuses |
| **Convergence** | Every substantive change needs another agent's review; the debate converges after any turn once every agent has made or reviewed the latest one, including in Round 1 |
| **Scope creep blocked** | New ideas go to Parking Lot unless required for the fix |

## Configuration

Default agents: `sonnet` (Claude Sonnet) + `opus` (Claude Opus), both via your Claude Code login. Built-in aliases:

| Alias | Model | Runs through | Effort |
|-------|-------|--------------|--------|
| `fable` | Claude Fable 5.1 | Claude Code login | low/medium/high |
| `opus` | Claude Opus | Claude Code login | low/medium/high |
| `sonnet` | Claude Sonnet | Claude Code login | low/medium/high |
| `codex` | Codex | Codex CLI | low/medium/high |
| `gemini` | Gemini (auto) | Gemini CLI | - |
| `copilot` | Copilot CLI | Copilot CLI | - |

3-agent debates: `--agents sonnet,opus,fable`. Any alias takes a model override: `--agents opus:claude-opus-5-5,sonnet`.

Details:

- **Cadence:** `--agents opus,sonnet,fable@2` makes `fable` take full turns in rounds 1, 3, 5, … An agent off its cadence can still sign off with a cheap confirm-only turn, so convergence does not wait for its next round.
- **Confirm-only turns:** once the Proposal is `STATUS: CONVERGED` with no `OPEN` dispute, an agent that already took a turn is sent only the diff since its last turn (20 KB at most) and answers `CONFIRM` or `OBJECT: <reason>`. `CONFIRM` costs cents, not a full turn; anything else falls back to a full turn.
- **Agent tools:** `--agent-tools "Read,Grep,Glob,Bash(pytest)"` replaces the `--allowedTools` value of every agent that has one. The agent is told its tool list.
- **Hooks:** the Claude aliases pass `--settings '{"disableAllHooks": true}'`, so the host's plugin hooks do not run inside agent turns.
- **Rewritten lines:** a one-character slip in an earlier tagged line is restored automatically (same tags, near-identical text, one match); anything else still rejects the turn.
- **Effort** is set per alias in the config (`reasoning.default`, default `high`). There is no command-line flag. Lower it to `medium` for faster, cheaper Claude turns.
- **`{SCRIPT_DIR}`** in a `command_template` expands to the directory holding provider_runner.py, so an installed config can reference scripts in `~/.agent-debate/`.
- **Fable** needs Claude Code 2.1.251 or newer. Check with `claude --version`; update with `npm install -g @anthropic-ai/claude-code@latest`.
- **Cost:** each debate writes a `.state.json` next to the debate file with per-turn usage and cost. Claude turns record Claude Code's estimate.
- **Transcripts:** each provider turn writes its full prompt and raw reply to `<debate-file-stem>.turns/<attempt-id>.{prompt,stdout,stderr}.txt`, keyed by the same attempt ID used in `.state.json`.
Agent count limits come from `debate.max_agents` in config (default in this repo: 4). Override per-project with a `debate.config.json` in your project root. Edit `~/.agent-debate/config.json` to add custom aliases or adjust min/max agent limits.

Every alias must declare `metering`: paid Claude calls use provider-reported cost, Codex uses configured token pricing, and intentionally unmetered adapters use `{"mode": "none"}`. Debate state records accepted and rejected calls; missing required accounting fails visibly.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/gumbel-ai/agent-debate/main/install.sh | bash -s -- --uninstall
```

## Known limitations

- Running the orchestrator from inside a Claude Code session works. Pass `--skip-provider claude` only if you want to write Claude's turns yourself instead of spawning a second Claude.
- Claude Code prints a cost estimate for every turn and the state file records it. On a subscription seat that is an estimate, not a charge.
- Tests: `python -m unittest discover -s tests`. On Windows the tests that exec `.sh` files directly fail with WinError 193; the pure-Python tests pass.
- This project orchestrates CLIs; reliability depends on local provider CLI behavior, auth state, and model availability.
- Copilot CLI auth env precedence: `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then `GITHUB_TOKEN` (with Copilot Requests permission).
- Copilot Free tier is 50 premium requests/month; each Copilot turn consumes one request.
- Host-direct turns run outside the orchestrator process, so their usage is not included in orchestrator cost totals.
- Auto mode validates structure and tags, but final judgment is still human.

## License

MIT
