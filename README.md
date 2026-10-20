# context-diet

Measure and cut what Claude Code loads before your first prompt (built-in tool schemas, the skills listing, CLAUDE.md, MCP), then prove with an A/B that your skills still get picked.

S-T: every session pays a fixed "entry fee" of tokens before you type anything. This repo tells you what's in that fee, helps you shrink it, and checks you didn't break anything doing it. Example: hiding 20 rarely used skill descriptions saves ~2k tokens on every call; the A/B tells you if Claude still finds those skills when you need them.

## What's here

| Folder | What it does |
|---|---|
| `measure/` | `start_tokens.py` (real start per session from your logs, split by block), `kiro_footprint.py` (Kiro steering/agent footprint and live probe), `probe-interactive.sh` (cost of one built-in tool in a real interactive session), `ablate.sh` (headless component ablation) |
| `audit/` | `skill-usage.py` (how often each skill really fired), `audit.py` (suggested `skillOverrides`), `skill-lint.py` (600-char description cap), `mcp-usage.py` (calls per MCP server, suggests `deniedMcpServers`), `size-ratchet.py` (fails when CLAUDE.md, agents or a skill listing grow past a pinned baseline), `memory-lint.py` (lint CLAUDE.md and its `@` imports for dangling pointers, import cycles and duplicated rules), `handoff-lint.py` (fail when a command or skill hidden from the model is referenced as a model handoff) |
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
python3 audit/mcp-usage.py --all-projects          # which MCP servers to deny
python3 audit/size-ratchet.py --baseline ~/context-diet-baseline.json   # pin sizes; exit 1 when they grow
python3 audit/memory-lint.py                       # dangling @imports and paths in CLAUDE.md
python3 audit/handoff-lint.py                      # hidden commands/skills that other files tell the model to run
```

Scope and config: scripts read `$CLAUDE_CONFIG_DIR` if set, else `~/.claude`. Session readers (`start_tokens.py`, `skill-usage.py`, `mcp-usage.py`, `ab/mine.py`) default to the current directory's project; use `--project PATH` for another one or `--all-projects` for all. If the project has no sessions they exit 2 rather than reading everything. Anything that calls a model (`ablate.sh`, `ab/filter.py`, `ab/synth.py`, `ab/run.py`, `ab/quick_ab.py`) requires `--model` with an explicit model id.

Then follow `docs/RUNBOOK.md`: change one lever, A/B it (`ab/README.md`), keep it only if recall holds.

## Findings that shaped it

- `claude -p` starts ~21k lower than an interactive session. Measure real starts (`entrypoint: cli`).
- The Artifact tool alone is ~12-16k tokens. `CLAUDE_CODE_DISABLE_ARTIFACT=1` in your shell rc turns it off; `enableArtifact: false` in settings.json can't be overridden and hot-reloads into live sessions.
- Never measure with `--disallowedTools ToolSearch`: every deferred MCP schema loads.
- `name-only` skills are picked by name alone. Renaming 7 opaque names to trigger-shaped ones took recall on fresh prompts from 49% to 90% (McNemar p<0.0001). A `use-this-skill-when-` prefix added nothing.
- Built-in skills obey `skillOverrides` too.
- "Never used" is not "never needed". Hiding 25 never-used skills in one repo cut recall on prompts written for them from 17% to 9% and saved about 100 tokens, because the listing budget refills. `audit.py` only suggests hides with `--hide`; A/B them.

## Kiro

```sh
python3 measure/kiro_footprint.py WORKSPACE --home "$(mktemp -d)"      # static: steering, agent file:// resources, personal steering
python3 measure/kiro_footprint.py WORKSPACE --live --prompt "..." --agent NAME --expect REGEX --read-pattern REGEX
```

Counts `.kiro/steering/*.md` and the `file://` `resources` of `.kiro/agents/*.json` (tokens ~chars/4), and flags a file loaded both ways as a duplicate: it is counted twice. A personal `~/.kiro/steering` silently joins every session, so it is reported separately; pass `--home` with an empty dir (the live probe uses a fresh temp HOME by default) or the output warns that the real HOME was read. The live probe needs `kiro-cli` on PATH (skipped with exit 0 otherwise), runs one headless v3 chat under a hard `--timeout`, and reports the session's contextFiles tokens, whether the answer matched, and how many tool calls touched files matching `--read-pattern`. An isolated HOME has no login: point `--home` at a prepared one if your setup needs auth.

## Hidden skill handoff lint

A command or skill hidden from the model (`disable-model-invocation: true`, or `user-invocable-only` / `off` in `skillOverrides`) can't be invoked by Claude. If another command, skill, agent or CLAUDE.md says "then run /ops:deploy", the model hits a dead end.

S-T: hiding `/ops:deploy` is fine, but a line telling the model to run it is a promise nobody can keep. Allowed: "ask the user to type /ops:deploy", "the user can run /ops:deploy". Not allowed: "then invoke /ops:deploy", or "you run /ops:deploy" (agent prompts say "you" to the model). Each reference is judged on its own.

```sh
python3 audit/handoff-lint.py                                    # hidden set from frontmatter under .claude/
python3 audit/handoff-lint.py --settings ~/.claude/settings.json # also skillOverrides that hide
python3 audit/handoff-lint.py --hidden hidden.json --json        # explicit ids: ["ops:deploy"] or {"commands": ["ops/deploy"]}
```

Scans `.claude/commands`, `.claude/skills`, `.claude/agents` and `CLAUDE.md` under `--root` (override with `--scan`). `--prefix`, `--separator`, `--namespace` and `--bare` tune what counts as a reference. Exit 1 on any finding, 2 on usage errors. Run it right after you hide something (`docs/RUNBOOK.md`).

## Tests

```sh
python3 -m unittest discover -s tests
python3 -m unittest discover -s ab/tests
```

MIT licensed.
