#!/usr/bin/env python3
"""OpenRouter debater for agent-debate.

Reads the debate prompt from stdin, sends it to one OpenRouter model, and prints
the model's reply to stdout (which the orchestrator captures as the agent's turn).

Usage (set as a config.json alias):
    command_template: ["<python>", "or_agent.py", "--model", "z-ai/glm-5.2"]
    prompt_transport:  "stdin"

Key resolution order: $OPENROUTER_API_KEY, else OPENROUTER_API_KEY= line in
a .env file next to this script. No external deps (stdlib urllib only).
"""
import argparse, json, os, sys, urllib.request

# Windows pipes default to cp1252; model output is UTF-8 (arrows, em-dashes, etc.)
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
URL = "https://openrouter.ai/api/v1/chat/completions"


def get_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key.strip()
    try:
        with open(ENV_FILE, encoding="utf-8-sig") as f:
            for line in f:
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    sys.exit("or_agent: no OPENROUTER_API_KEY in env or " + ENV_FILE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", default=None)  # accepted/ignored; OpenRouter handles reasoning per-model
    args = ap.parse_args()

    prompt = sys.stdin.read()
    body = json.dumps({
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {get_key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:  # debates can be long; generous timeout
        data = json.load(r)
    sys.stdout.write(data["choices"][0]["message"]["content"])


if __name__ == "__main__":
    main()
