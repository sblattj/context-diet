# Context Diet Runbook

How to measure what Claude Code loads before you type anything, cut it, and prove the cuts didn't change which skills Claude picks.

"Context diet" means shrinking the fixed prefix every request carries: system prompt, tool definitions, memory files, MCP instructions and the skills listing. You pay it on every turn of every session, and again for every subagent you spawn.

This was written from one real pass on a heavy setup (74 skills, 8 MCP servers, a long global CLAUDE.md), then generalized so you can run it anywhere. Measured on Claude Code 2.1.29x with Sonnet, Oct 2026. Token figures are exact first-turn usage unless marked as chars / 4.

## Headline numbers

| What | Before | After | Notes |
|---|---|---|---|
| Subagent start | 42.3k | 9.6k | workflow subagent before, `lean` subagent after (-77%); with a small CLAUDE.md, see step 4 |
| Interactive session start | 53.8k | 34.7k | -35%, every session, every turn |
| Same, plus 11 rare built-in skills to name-only | 34.7k | 32.9k | see step 7 |
| Fresh start, Artifact tool off vs on | ~45k | ~30.2k | see step 9 |
| Skills listing | 31.5k chars | 20.2k chars | about -2.8k tokens in a real session |
| Skill recall (448 replayed real prompts) | 78% | 78% | McNemar p = 1.0 |
| Recall on renamed name-only skills | 49% | 90% | McNemar p < 0.0001, see step 8 |

S-T: think of the context as a backpack Claude packs before every trip. We weighed each pocket, threw out what was never used, and kept the heavy tool manuals only for the workers that need them.

### Where it went

In a real interactive session, about 37.7k of a 49k start was the system prompt plus built-in tool definitions. No setting trims most of that in a main session. You avoid it by giving a subagent fewer tools (step 4) or by turning off one huge tool (Artifact, step 9). Everything else settings can trim lives in the smaller blocks:

| Block in a real interactive session | Before | After | Change |
|---|---|---|---|
| Skills listing | 31,494 chars | 20,164 chars | -2.8k tok |
| CLAUDE.md / AGENTS.md | 15,857 chars | 10,618 chars | -1.3k tok |
| MCP server instructions | 7,159 chars | 5,410 chars | -0.4k tok |
| Deferred tool names | 4,920 chars | 5,620 chars | +0.2k tok |
| Agent type listing | 4,607 chars | 4,858 chars | +0.1k tok (lean added) |
| First-turn input, exact | 53,849 | 49,312 | -4,537 |

Block changes are chars / 4, so approximate. Totals are exact. "After" is four fresh sessions (49,109 to 49,313), still with the Artifact tool on. Switching it off took the start to 34.7k.

## What fills the context

| Part | What it is | Controlled by | Can you cut it? |
|---|---|---|---|
| Built-in tools | JSON schemas for Agent, Workflow, Task*, Cron, Web*, Artifact and so on. Some run past 1.5k tokens each. | `--tools`, `--disallowedTools`, agent `tools:` allowlist, env vars | Per context. A subagent can drop them; a main session usually needs most of them. |
| Floor | Harness system prompt plus Bash, Read, Edit, Write, Grep, Glob | Nothing | No. About 6.6k. |
| MCP | With tool search on, one line per deferred tool plus each server's instructions block | MCP config, `deniedMcpServers`, `disableClaudeAiConnectors` | Yes, drop servers you don't use |
| Memory | `~/.claude/CLAUDE.md`, project CLAUDE.md files, anything they @import | The files | Yes. Move procedures into skills, keep pointers. |
| Skills listing | Name plus description (and `when_to_use`) of every model-visible skill, built-ins included | `skillOverrides`, the description text, the skill name | Yes. Usually the cheapest, safest win. |

A subagent pays the whole prefix again when it starts. Fan out ten general-purpose agents and you pay roughly 350k tokens of setup before any of them reads a file.

Paths below use `~/.claude`. If you set `CLAUDE_CONFIG_DIR`, every script here reads that directory instead.

## Measure first

Get the real start number before you change anything. Two methods, used for different things.

### Real interactive sessions: your starting number

Your history already has the answer. For each main-session JSONL under `~/.claude/projects/<project>/`, take the first assistant record's usage: `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. Keep sessions whose first prompt is short and plain. The `attachment` records before that turn show each block's size (`skill_listing`, instructions, MCP instructions, agent listing).

- `measure/start_tokens.py` does this over your session logs and prints the total, per-block sizes and the median start. By default it reads only the current project's sessions (run it from the project you care about); `--project PATH` picks another, `--all-projects` reads all. Resumed or compacted sessions ("This session is being continued...") and non-`cli` entrypoints are left out of the list and the median.
- `measure/probe-interactive.sh` opens a fresh interactive session with a one-line prompt (optionally with extra flags like `--disallowedTools X`) and reads its first turn. Use it for an exact "after" number.

`/context` also works, but it reports per-category estimates.

### Headless ablation: what each part costs

`measure/ablate.sh -m MODEL_ID` (the model is required; name an explicit id) runs a trivial prompt with `claude -p --output-format json`, then switches off one part at a time (MCP, memory, skills, everything down to the floor). The drop is that part's cost. Results land in a JSONL you can tail.

- MCP = base minus no-mcp
- memory = base minus no-memory (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`)
- floor = floor run minus memory
- built-in tools = what's left

Use `-p` for quick deltas only. Never report it as your start. Headless runs skip about 20k of interactive-only context (we saw 28.0k headless vs 49k interactive). The logged blocks are about the same size in both modes; the gap is system prompt and tools.

### Measure a real subagent too

Each agent type starts at a different size: 42.3k for a workflow subagent before, 34.8k general-purpose and 9.6k lean after. Compare like with like. Spawn one with a one-word task, from a session started after your last change (one spawned from a session already open before a CLAUDE.md trim still measured the old size), and read the first assistant turn's usage from `~/.claude/projects/<project>/<session>/subagents/agent-*.jsonl`.

Noise is about +/-200 tokens run to run. Repeat any run whose delta is under 500, and always run from the same directory, since project CLAUDE.md files change the number.

S-T: weigh the backpack, take one pocket out, weigh again. The difference is that pocket. Example: 30,546 with all MCP servers and 25,803 with none means MCP weighs about 4.7k.

## Steps

### 1. Back up

Every change is a file edit, so rollback is a copy back.

```sh
B=~/.claude/backup/context-diet-$(date +%Y%m%d); mkdir -p $B
cp ~/.claude/settings.json ~/.claude/CLAUDE.md $B/
cp -R ~/.claude/skills $B/skills
# rollback: copy back, then start a new session
```

Settings, agent files, skill tiers and MCP denials show up in new sessions. A restart is also how you confirm a rollback worked.

### 2. Measure

Run "Measure first" above. Write down the real interactive start, the ablation parts, and one subagent start.

If your settings.json or CLAUDE.md is generated from another source (a dotfiles render step, a symlinked template), run that render first so the baseline measures what is really installed. Make every later change in the source and re-render; an edit to the rendered file is overwritten on the next render.

### 3. Mine your usage

Every session is a JSONL under `~/.claude/projects/`. Two signals per skill:

- Auto: an assistant `tool_use` block named `Skill`, with `input.skill` naming it. Claude picked it.
- Typed: a user message containing `<command-name>/name</command-name>`. You asked for it.

`audit/skill-usage.py` counts both per skill (recency window `--window-days`, default 30). It reads the current project's sessions by default; skill usage is global, so pass `--all-projects` for tiering decisions. It does not count MCP calls; `audit/mcp-usage.py` does. It reads the same transcripts with the same scoping flags (`--project`, `--all-projects`, `--window-days`, headless runs skipped), counts `mcp__<server>__<tool>` calls per server (a message split over several lines counts once), and joins them with the servers in your config (`~/.claude.json`, or `$CLAUDE_CONFIG_DIR/.claude.json`, plus the project's `.mcp.json`; override with `--config`):

```sh
python3 audit/mcp-usage.py --all-projects          # table + deniedMcpServers block
python3 audit/mcp-usage.py --all-projects --json   # or --csv
```

Columns: server, calls in the window, last used, configured (yes/no), suggestion (`deny` for a configured server with 0 calls, else `keep`). Connector names with spaces or dots ("claude.ai Some Connector") show up in tool names as `claude_ai_Some_Connector`; the tool maps them back. Servers that were called but are not in a config file (claude.ai connectors, plugin servers) show `configured: no` and are listed under the table. Read-only.

Filter out your own test runs. After a few A/B rounds, one skill went from 4 uses to 59. Skip headless sessions (`"entrypoint":"sdk-cli"`) whose first prompt matches a test prompt, and run A/Bs from a dedicated directory so you can filter by project path.

On our setup, 30 days showed 17 visible skills never used and 13 more used once or twice. That's the target list.

### 4. Add a lean subagent

Biggest single win. A custom agent type with a short tool allowlist skips built-in schemas, MCP and the skills listing. Ours starts at 9.6k vs ~43k for the old workflow subagent. That 9.6k assumes a small CLAUDE.md: global memory loads into every subagent, lean ones included, so your CLAUDE.md adds on top. On a second setup a lean subagent started at 19.7k with a 36k-byte CLAUDE.md, and at 14.1k after trimming it to 19.5k bytes (step 10). Copy `agents/lean.md` to `~/.claude/agents/lean.md`:

```markdown
---
name: lean
description: Low-context worker for workflow fan-out and mechanical tasks. Only shell and file tools, no skills, MCP or publishing. Use when a subagent just needs to read, grep, edit or run commands.
tools:
  - Bash
  - Read
  - Edit
  - Write
  - Grep
  - Glob
---

You are a focused worker. Do the task with shell and file tools only, then return the result as your final message.
```

Then make it the default in your global CLAUDE.md:

```markdown
- Default fan-out to the low-context `lean` agent (`subagent_type: lean`, `agentType: 'lean'` in workflows):
  shell + file tools only, about 10k tokens at start vs 35k to 43k. Use a heavier type only when the agent needs MCP, skills, web or publishing.
```

A new agent type isn't loaded into the session that created it. Start a new session.

S-T: a general-purpose subagent carries the whole toolbox to tighten one screw. The lean one carries a screwdriver. Ten parallel grep agents cost about 96k at start instead of about 423k.

### 5. Tier the skills

`skillOverrides` in `~/.claude/settings.json` sets how much of each skill sits in context:

| State | In context | Claude can call it | You can type /name | Use for |
|---|---|---|---|---|
| `on` (default) | name + description | yes | yes | Skills used more than ~3 times a month, and look-alikes that need their scope spelled out |
| `name-only` | name only | yes | yes | Rare skills with a self-explaining name; new skills on probation |
| `user-invocable-only` | nothing | no | yes | Skills you only ever type |
| `off` | nothing | no | no | Retired skills |

```json
{
  "skillOverrides": {
    "release-status": "on",
    "deploy-helper": "name-only",
    "copy-output": "user-invocable-only",
    "old-experiment": "off"
  }
}
```

Decision rules (from step 3 counts):

- Typed but never auto-picked: `user-invocable-only`.
- More than 3 uses in 30 days: `on`.
- 1 to 3 uses: `name-only`.
- 0 uses and older than 30 days: `user-invocable-only`.
- Created in the last 30 days: `name-only` (probation), never hidden. Promote after 3 uses.
- Keep any decision you made by hand. The rules only suggest.
- Age is when git first added the skill, not the file time: a fresh clone or checkout makes every file look new.

`audit/audit.py` prints these suggestions with `--hide`. It doesn't apply them. Without `--hide` it only suggests listing skills again, because "never used" is not "never needed": in one repo, a batch of 25 never-used skills went name-only and recall on prompts written for them fell from 17% to 9%, for a saving of about 100 tokens (the listing budget refilled). A/B every hide (`ab/README.md`) and keep only the ones where recall holds.

Watch out for look-alikes. Hiding all but the top 15 took wrong picks from 2% to 7%, because Claude guessed a similarly named skill. Keep siblings (two "status" skills, two "review" skills) fully listed.

Never name a hidden skill in a listed description. "Use standup-sync instead" made Claude call the hidden skill, which fails with `disabled for model invocation`. Write "the user command /standup-sync (suggest it, do not call it)".

### 6. Cap descriptions at 600 characters

A listed description is paid in every session and every subagent that has the Skill tool. Ours had 13 over 800 characters, one at 1,273.

- Keep: what it does in one sentence, the distinctive trigger phrases, one scope sentence if a sibling exists.
- Move the rest of the triggers to a `## More triggers` line in the body. Claude reads it after loading the skill.
- Add a scope sentence when a skill steals prompts. "Read-only, one item: not for creating items or listing many" cut false fires on one skill from 5/6 to 2/6. "Writes the standup only: syncing it is the user command /standup-sync" took misfires to 0/6.
- Name-only skills don't count, since their description isn't loaded.

Result: trimming 9 long descriptions to 600 chars or less kept every one firing on plain-word prompts (27/27 over 3 reps).

`audit/skill-lint.py --settings ~/.claude/settings.json` flags over-cap descriptions on fully listed skills; `audit/audit.py` also flags listed descriptions that name a hidden skill.

### 7. Tier the built-in skills too

Built-in skills (`dataviz`, `claude-api`, `update-config`, `/loop`, `/schedule`, `/init` and friends) sit in the same listing and obey `skillOverrides` like your own. Most people never touch them.

Setting 11 rarely used built-ins to `name-only` took the interactive start from 34.7k to 32.9k. They stay callable and typeable. Check which built-ins you actually use in step 3's counts before picking.

```json
{ "skillOverrides": { "dataviz": "name-only", "claude-api": "name-only", "init": "name-only" } }
```

### 8. Give name-only skills trigger-shaped names

For a name-only skill, the name is the only thing Claude sees. An opaque name means it never gets picked.

We renamed 7 opaque names to names that say when to use them, for example `boil-the-ocean` -> `going-all-in-or-about-to-claim-done`. On fresh prompts, recall for those skills went from 49% to 90% (McNemar p < 0.0001). Other skills stayed flat.

What didn't help:

- A `use-this-skill-when-` prefix added nothing over the same names without it, and cost ~300 start tokens.
- Skills that already had clear names lost ground when renamed. Only rename the opaque ones.

Renamed skills can't be typed by their old name, so tell yourself (and update any CLAUDE.md pointers). `ab/make_rename_arms.py` builds the before/after arms for this A/B.

### 9. Turn off the Artifact tool

Some built-in tools have huge definitions, and headless runs don't load them, so `-p` ablation never shows them. Probe real sessions instead: `measure/probe-interactive.sh --disallowedTools X` vs a plain run. On our setup the Artifact tool (publishes pages to claude.ai) was 14.6k of a 49.3k start. A fresh start is ~30.2k with it off vs ~45k on. Removing any one smaller tool moved the total 3k to 5k, too close to the noise to trust one at a time.

Turn it off with an env export in your shell rc:

```sh
# ~/.zshrc (or ~/.bashrc)
export CLAUDE_CODE_DISABLE_ARTIFACT=1
```

Don't use `"enableArtifact": false` in settings.json. Nothing overrides that key, and running sessions hot-reload it. So "off" reaches live sessions, but "on" never does, and you can't get the tool back for one publish.

To publish, use a throwaway side session that starts without the export: `tools/publish-artifact.sh`. `tools/artifacts.sh` handles the related artifact chores. Your normal sessions stay lean.

Never disallow ToolSearch. Every MCP schema then loads in full. Our start went from 49k to 101k.

### 10. Shrink CLAUDE.md

Global memory loads in every session and subagent. Keep invariants there (rules that must hold everywhere). Move procedures (how to do X) into skills and leave a one-line pointer.

```markdown
# before: 40 lines on tiers, coverage checks, delegation handoff (about 0.9k tokens)
# after:
- For code discovery prefer the graph tools over grep; grep for literals and configs.
- Before structural claims or delegating code research, load the `code-graph` skill.
```

- Set the pointed-to skill to at least `name-only`. A `user-invocable-only` skill can't be loaded by Claude.
- Check whether an MCP server already injects the same guidance via its instructions block. Ours did.
- Measured: one 2.4 KB block became 3 lines and saved 925 tokens per context.

Blocks between installer markers (`<!-- tool:start -->` ... `<!-- tool:end -->`) may get rewritten on upgrade. Keep the markers and re-check after upgrades.

After shrinking, lint the always-loaded files so the savings do not silently erode:

```sh
python3 audit/memory-lint.py                  # global CLAUDE.md and its @imports
python3 audit/memory-lint.py --project DIR    # plus DIR/CLAUDE.md and DIR/.claude/CLAUDE.md
python3 audit/memory-lint.py FILE...          # explicit files only; add --json for machine output
```

Rules, each with file:line and a fix hint:

- ML001 error: an `@` import whose target is missing.
- ML002 warning: a backticked or linked file path that does not exist.
- ML003 warning: an import cycle.
- ML004 warning: the same instruction in more than one place (40+ chars, compared after stripping bullets and emphasis, case and spacing).
- ML005 info: a file over 60 lines with no pointer to a skill or another file. Move procedures into skills.

Code fences are ignored. Exit 1 only on ML001. There is deliberately no score or size threshold.

### 11. Prune MCP servers

With tool search on (the default), each MCP tool costs one line until used, but every server's instructions block loads in full. Count calls per server with `python3 audit/mcp-usage.py --all-projects` (step 3) and drop the zeros. It prints a ready-to-paste block for the servers it suggests denying:

```jsonc
"deniedMcpServers": [{"serverName": "claude.ai Idle Thing"}]
```

Check the list before pasting: a server you only need seasonally, or one that has been idle because it was broken, also shows 0 calls.

```jsonc
// ~/.claude/settings.json: block individual claude.ai connectors by name
{ "deniedMcpServers": [ { "serverName": "claude.ai Google Drive" } ] }

// or turn every claude.ai connector off
{ "disableClaudeAiConnectors": true }

// local servers: claude mcp remove <name>, or move them to a project .mcp.json
```

Denying two unused connectors saved 1.6k per context. Their tool lines were small; their instruction blocks were the cost. Some connectors back built-in features, so check what one does before you deny it.

### 12. Prove it with an A/B

Hiding, trimming or renaming skills is only safe if Claude still picks the same skills. Replay your own real prompts. The pipeline lives in `ab/` (see `ab/README.md`).

Build the cases (`ab/mine.py`, `ab/filter.py`, `ab/synth.py`):

- Positives: every user turn followed by an auto Skill call. The label is that skill. If several fired, store them all as an accept set.
- Negatives: short prompts where no skill fired. Used for false fires.
- Filter: a cheap model keeps only prompts that make sense standalone. "yes" and "do 2 and 3" are noise.
- Add a few synthetic prompts for skills with no history.

Run the arms (`ab/make_arms.py`, `ab/make_rename_arms.py`, `ab/run.py`). Each arm is a settings file with a full `skillOverrides` map. Every case runs under every arm with read-only tools, from a dedicated cwd:

```sh
claude -p "$PROMPT" --model sonnet --settings arms/B.json \
  --tools Skill Read Grep Glob --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --max-turns 2 --output-format stream-json --verbose
```

Score it (`ab/report.py`):

| Metric | Definition | Why |
|---|---|---|
| Recall | positive cases where a skill in the accept set fired | main safety number |
| Wrong pick | a skill fired, but not one in the accept set | look-alike confusion |
| False fire | a negative case fired any skill | greedy descriptions |
| Start tokens | median first-turn input per arm | the saving you're buying |

- Wilson 95% intervals; at 100 to 200 cases they're about +/-7 points wide.
- Pair cases across arms and run an exact McNemar test on discordant pairs. Our main diet: 1 case only A hit, 2 only B hit, p = 1.0.
- Leave skills hidden in an arm out of that arm's recall.
- 2 reps per arm for a whole-config change; 3 reps on a small case file for one description fix.
- About 450 runs and 20 minutes per round at 8 parallel jobs.
- `ab/quick_ab.py` is the small version for a single-description check.

Also build the control arm (`make_rename_arms.py ... --control`, see `ab/README.md`) so the report carries a `control ...: harness can detect loss: yes/no` line. If it says `no`, do not read "no regression" as a pass: the prompt set cannot detect loss.

Most "wrong picks" were label noise. Read every miss before editing a description. And grep the file to confirm your edit landed before running the "after" arm.

### 13. Keep it lean

Skills pile up. Five new ones arrived during this pass and pushed the baseline back up.

- New skills start `name-only`, with a trigger-shaped name and a description of 600 chars or less. Promote after real use.
- Monthly audit: re-run `audit/skill-usage.py`, `audit/audit.py` and `audit/skill-lint.py`, and record the baseline with `measure/start_tokens.py`. Read-only: it suggests, you apply.
- Pin what you saved so it cannot creep back: `python3 audit/size-ratchet.py --baseline ~/context-diet-baseline.json [--project DIR]`. The first run writes the baseline. Later runs print `GREW`/`NEW` lines and exit 1 when CLAUDE.md (imports included), `agents/*.md`, or a skill's listing cost grew; `SHRANK`/`GONE` do not fail. After a deliberate change, re-pin with `--update`. Run it from a hook or CI.
- Schedule it with launchd, cron or systemd. Those don't load your shell profile, so set PATH inside the script and call the `claude` binary, not a shell function.

### 14. Optional: a hint hook

If you want to hide more than recall allows, a `UserPromptSubmit` hook can rank all skills against each prompt and inject a one-line hint only when one clearly matches.

- Pure-Python BM25 over name, description and `## More triggers` ranks 70 skills in about 6 ms.
- Offline, the right skill was top 3 for 22 of 22 prompts, but the margin was thin: off-topic prompts scored up to 7.1, the weakest real hit 7.8. Tune on real negatives.
- Gate on an absolute score plus a top-1/top-2 gap. Most prompts get no hint (0 tokens).

Build it only if step 12 shows recall dropping. On our setup, name-only skills with good names kept firing without it.

## Traps

| Symptom | Cause | Fix |
|---|---|---|
| "Lean" run costs more than baseline (42k) | `--tools` without ToolSearch loads every MCP schema in full | Empty MCP config, or include ToolSearch |
| Disallowing ToolSearch: 49k -> 101k | Same thing, in a main session | Never disallow ToolSearch |
| Headless says 28k, real sessions start at 49k | `-p` skips interactive-only context (agent listing, output style, built-in and plugin skills, Artifact) | Report real first-turn usage; use `-p` only for per-part deltas |
| "no-skills" run costs more than baseline | With skills off, Claude Code inlines the `workflow-authoring` skill (about 17k chars) into the Workflow tool, which outweighs the listing it dropped | Compare `no-workflow` with `no-skills` (`ablate.sh` does both) |
| "Lean tools" and "no skills" look the same | Leaving `Skill` out of `--tools` also drops the listing | Change one thing per run |
| Subagent numbers don't line up | Agent types start at different sizes (workflow 42.3k, general-purpose 34.8k, lean 9.6k), and every type adds your CLAUDE.md on top | Compare the same type; read usage from the transcript |
| Subagent didn't shrink after a CLAUDE.md edit | It was spawned from a session open before the edit (ours: 19.9k from the old session, 14.1k from a new one) | Spawn it from a new session |
| Baseline or "after" number looks unchanged | settings.json or CLAUDE.md is rendered from another source (a dotfiles generator), and the render hasn't run, or it overwrote your edit | Run the render step before measuring, and edit the source, not the rendered file |
| Artifact tool won't come back on | `enableArtifact: false` in settings wins over everything and hot-reloads into live sessions | Use the `CLAUDE_CODE_DISABLE_ARTIFACT=1` env export; publish via `tools/publish-artifact.sh` |
| First session after re-enabling a tool still lacks it | Pre-warmed sessions keep the settings they started with | Open a second new session |
| Usage counts jump after testing | A/B and ablation runs write session logs | Filter headless test sessions and the A/B cwd |
| `disabled for model invocation` errors | A listed description names a hidden skill | Point at "the user command /x" |
| Wrong picks rise after hiding | Look-alike skills reduced to names | Keep siblings fully listed, add a scope sentence |
| Name-only skill never fires | Its name doesn't say when to use it | Trigger-shaped rename, then A/B it |
| Old `/name` stops working | Skill was renamed | Use the new name; update pointers |
| A/B "after" equals "before" | Edit didn't apply | Grep the file before running |
| Token medians differ between rounds | Run directory changed | Compare only arms within one round |
| `Agent type 'lean' not found` | Agents load at session start | New session |
| Audit hides a skill CLAUDE.md points to | Typed-only rule fires on a pointer target | Keep explicit name-only decisions |
| Baseline creeps back up | New skills default to fully listed | Name-only probation plus monthly audit. Pin sizes with `audit/size-ratchet.py` (step 13); it fails when any tracked file or skill listing grows |
| Scheduled audit can't find `claude` | launchd/cron skip your shell profile | Set PATH in the script |
| `zsh: no matches found` | Unquoted `--include=*.jsonl` got globbed | Quote it |

## Checklist

- [ ] Back up settings.json, CLAUDE.md and the skills folder.
- [ ] Record the real start: `measure/start_tokens.py` on recent short-prompt sessions.
- [ ] Run `measure/ablate.sh -m MODEL_ID`; write down base, MCP, memory and floor.
- [ ] Spawn one default subagent and read its first-turn usage.
- [ ] Mine 30 days with `audit/skill-usage.py --all-projects`. Filter out test runs.
- [ ] Install `agents/lean.md`, add the default-to-lean rule to CLAUDE.md. Restart.
- [ ] Apply skill tiers from the rules (`audit/audit.py`). Keep look-alikes listed.
- [ ] Set rarely used built-in skills to `name-only`.
- [ ] Trim listed descriptions to 600 chars; overflow to `## More triggers`; scope sentences on greedy skills.
- [ ] Run `audit/skill-lint.py --settings ~/.claude/settings.json` (or your `$CLAUDE_CONFIG_DIR`) and `audit/audit.py`; reword any listed description that names a hidden skill.
- [ ] Rename opaque name-only skills to trigger-shaped names; A/B with `ab/make_rename_arms.py`.
- [ ] Export `CLAUDE_CODE_DISABLE_ARTIFACT=1` in your shell rc (not the settings key); publish via `tools/publish-artifact.sh`.
- [ ] Probe other heavy built-in tools with `measure/probe-interactive.sh --disallowedTools X`.
- [ ] Move procedures out of CLAUDE.md into skills; leave one-line pointers. Run `audit/memory-lint.py` and fix any ML001.
- [ ] Deny or remove MCP servers with zero calls (`audit/mcp-usage.py --all-projects`).
- [ ] A/B new settings against old on real prompts (`ab/`); adopt only if recall holds and wrong picks don't rise.
- [ ] Build the A/B control arm (`ab/make_rename_arms.py --control`); the report must say `harness can detect loss: yes`.
- [ ] Pin sizes: `audit/size-ratchet.py --baseline FILE` (first run writes it), then run it from a hook or CI.
- [ ] Open a few fresh sessions and record the new real baseline.
- [ ] New skills start name-only; schedule the monthly audit.

Figures come from one heavy setup on Claude Code 2.1.29x with Sonnet. Your parts will weigh differently, which is why you measure first. Setting names (`skillOverrides`, `deniedMcpServers`, `disableClaudeAiConnectors`) come from current Claude Code docs; check them against your version.
