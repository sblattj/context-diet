---
name: measuring-real-session-start-tokens
description: "Get the real number of tokens a Claude Code session starts with, split by block (skills listing, CLAUDE.md, MCP, deferred tools, agents), and what each built-in tool costs. Use for: how big is my starting context, did the diet work, why does a session start at 49k."
---

Run from the plugin root (`${CLAUDE_PLUGIN_ROOT}`):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/measure/start_tokens.py              # newest 5 interactive sessions
python3 ${CLAUDE_PLUGIN_ROOT}/measure/start_tokens.py --last 20 --json
LAUNCH_SHELL="zsh -ic" PROBE_CWD=<trusted dir> ${CLAUDE_PLUGIN_ROOT}/measure/probe-interactive.sh Artifact Workflow   # per-tool cost
${CLAUDE_PLUGIN_ROOT}/measure/ablate.sh --list                      # headless component ablation
```

- Start = first assistant `usage` (input + cache_creation + cache_read). Only `entrypoint: cli` is a real start; `claude -p` reads ~21k low.
- Compare fresh sessions with a one-line prompt. Resumed or compacted sessions start with their summary.
- Never measure with `--disallowedTools ToolSearch`: every MCP schema loads (49k became 101k).
- Full method: `${CLAUDE_PLUGIN_ROOT}/docs/RUNBOOK.md`.
