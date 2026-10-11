# context-diet

Measure and cut what Claude Code loads before your first prompt (built-in tool schemas, the skills listing, CLAUDE.md, MCP), then prove with an A/B that your skills still get picked.

S-T: every session pays a fixed "entry fee" of tokens before you type anything. This repo tells you what's in that fee, helps you shrink it, and checks you didn't break anything doing it. Example: hiding 20 rarely used skill descriptions saves ~2k tokens on every call; the A/B tells you if Claude still finds those skills when you need them.

## What's here

| Folder | What it does |
|---|---|
| `measure/` | `start_tokens.py` (real start per session from your logs, split by block), `probe-interactive.sh` (cost of one built-in tool in a real interactive session), `ablate.sh` (headless component ablation) |
| `audit/` | `skill-usage.py` (how often each skill really fired), `audit.py` (suggested `skillOverrides`), `skill-lint.py` (600-char description cap) |
| `ab/` | Skill-trigger A/B: mine prompts from logs, build arms, synthesize missing positives, run, report recall / wrong pick / false fires / McNemar. Rename arms included |
| `tools/` | `publish-artifact.sh`: publish an Artifact page from a throwaway side session so the Artifact tool can stay off |
| `agents/lean.md` | A low-context subagent (shell + file tools only) for fan-out work |
| `skills/` | Plugin skills that point Claude at the above |
| `docs/` | `RUNBOOK.md` (the full method) and `CONTEXT-DIET.md` |

## Install as a plugin

```sh
claude plugin marketplace add sblattj/context-diet
claude plugin install context-diet@context-diet
```

Or just clone and run the scripts. Python 3.9+, no dependencies; `tmux` for the interactive probes.

## Quick start

```sh
python3 measure/start_tokens.py                    # what do my last 5 sessions in this project start at?
python3 audit/skill-usage.py --all-projects -o /tmp/usage.csv
python3 audit/audit.py /tmp/usage.csv --hide       # what to hide or list by name only (A/B before keeping)
```

Scope and config: scripts read `$CLAUDE_CONFIG_DIR` if set, else `~/.claude`. Session readers (`start_tokens.py`, `skill-usage.py`, `ab/mine.py`) default to the current directory's project; use `--project PATH` for another one or `--all-projects` for all. If the project has no sessions they exit 2 rather than reading everything. Anything that calls a model (`ablate.sh`, `ab/filter.py`, `ab/synth.py`, `ab/run.py`, `ab/quick_ab.py`) requires `--model` with an explicit model id.

Then follow `docs/RUNBOOK.md`: change one lever, A/B it (`ab/README.md`), keep it only if recall holds.

## Findings that shaped it

- `claude -p` starts ~21k lower than an interactive session. Measure real starts (`entrypoint: cli`).
- The Artifact tool alone is ~12-16k tokens. `CLAUDE_CODE_DISABLE_ARTIFACT=1` in your shell rc turns it off; `enableArtifact: false` in settings.json can't be overridden and hot-reloads into live sessions.
- Never measure with `--disallowedTools ToolSearch`: every deferred MCP schema loads.
- `name-only` skills are picked by name alone. Renaming 7 opaque names to trigger-shaped ones took recall on fresh prompts from 49% to 90% (McNemar p<0.0001). A `use-this-skill-when-` prefix added nothing.
- Built-in skills obey `skillOverrides` too.
- "Never used" is not "never needed". Hiding 25 never-used skills in one repo cut recall on prompts written for them from 17% to 9% and saved about 100 tokens, because the listing budget refills. `audit.py` only suggests hides with `--hide`; A/B them.

## Tests

```sh
python3 -m unittest discover -s tests
python3 -m unittest discover -s ab/tests
```

MIT licensed.
