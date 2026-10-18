# Skill-trigger A/B pipeline

Measures whether a skill-visibility change (for example moving skills to `name-only` in `skillOverrides`) still lets the model pick the right skill, using **real prompts mined from your own session logs**. Part of the context-diet kit (see [docs/CONTEXT-DIET.md](../docs/CONTEXT-DIET.md)). Python 3.9+, standard library only. Order: mine, filter, make_arms, synth (optional), run, report.

```
transcripts --mine.py--> candidates.jsonl --filter.py--> cases.jsonl --run.py--> results.jsonl --report.py--> tables
                                      skills dir --make_arms.py--> arms/*.json ----^
```

## Billing: unset `ANTHROPIC_API_KEY`

Every model call is `claude -p ...` in a subprocess. With `ANTHROPIC_API_KEY` set, Claude Code bills that API key; without it, your subscription login is used. `filter.py`, `run.py` and `quick_ab.py` **strip the key from the child environment by default**; `--keep-api-key` disables that. Every call has a hard wall-clock `--deadline` (the whole process group is killed) and bounded output (captured stdout is capped; `filter.py` also sets `CLAUDE_CODE_MAX_OUTPUT_TOKENS`).

## Model: always explicit

`filter.py`, `synth.py`, `run.py` and `quick_ab.py` require `--model` and have no implicit default. Name an explicit model id (`MODEL_ID` below); the bare alias `haiku` is rejected. Any other value is passed through to `claude --model`. Pin the same id across arms and rounds so results compare.

## Walkthrough

```sh
cd ab

# 1. Mine. Default root: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects.
python3 mine.py --all-projects --out work/candidates.jsonl --days 30 --max-negatives 150
```
Scope: without `--all-projects`, `mine.py` reads only the current directory's project (`<root>/<slug of cwd>`, slug = absolute path with every non-alphanumeric char turned into `-`), or the one named by `--project PATH`, and exits 2 if that project has no session dir. An explicit root (`mine.py /some/projects ...`) is scoped the same way; a directory that directly holds `.jsonl` files, or an explicit file, is read as-is.

A user prompt whose turn called `Skill(x)` is a positive labelled `x` (several skills in one turn: any is accepted). Short prompts whose turn called no skill are negatives. Positives that literally name the skill get `explicit: true`. Typed slash commands, tool results, meta/caveat messages and subagent (sidechain) turns are skipped; prompts are de-duplicated. Headless sessions (`claude -p`, SDK: any `entrypoint` other than `cli`) are skipped too, because those are your own A/B and eval runs; `--include-headless` keeps them.

```sh
# 2. Filter with a cheap judge (batches of 25; keeps prompts understandable without prior context)
python3 filter.py work/candidates.jsonl --out work/cases.jsonl --model MODEL_ID
```
Mined negatives are noisy (some no-skill turns *should* have used a skill): relabel by hand before trusting the negative fire rate.

**Selftest.** Before judging anything, `filter.py` runs the judge on 12 committed, clear-cut prompts (`ab/fixtures/selftest/filter.jsonl`: 6 self-contained requests it must keep, 6 replies or noise it must drop). It aborts with `ABORT: judge selftest failed ...` (exit 1) if agreement with the labels is below `--selftest-min` (default 10 of 12). Every disagreement is printed. This catches judge drift, which would otherwise silently bias which prompts reach the recall arms. `--selftest` runs only the check (exit 0 pass, 1 fail); `--no-selftest` skips it. The selftest costs 1 model call, reported in the final summary line (`N selftest calls`).

```sh
python3 filter.py --selftest --model MODEL_ID
```

```sh
# 2b. Synthetic positives for skills with no (or too few) mined positives (the issue added 14)
python3 synth.py work/cases.jsonl --skills-dir "$HOME/.claude/skills" --arms work/arms/*.json \
    --min-positives 1 --per-skill 2 --model MODEL_ID
```
Appends `"synthetic": true` positives (same schema, `explicit: false`) to `cases.jsonl`, generated from each skill's name and description through the same `claude -p` path as `filter.py` (your `--model`, deadline, key stripped). Prompts that name the skill, duplicates of existing cases, and skills hidden from the model (`user-invocable-only`/`off` in any `--arms` file, or `disable-model-invocation: true`) are skipped. `--min-positives` defaults to 1 and `--per-skill` to 2. `--dry-run` lists what would be generated. Run it after `make_arms.py` so `--arms` exists, and re-run `run.py` afterwards (it only runs the new cases). `report.py` excludes synthetic cases by default (`--synthetic include|only` to change) and prints their recall on a separate line, so real recall is not inflated.

**Selftest.** `synth.py` first generates 3 prompts for a synthetic fixture skill (`ab/fixtures/selftest/synth.json`). It aborts (exit 1) unless the reply parses, has exactly 3 non-empty prompts, and none contains the skill's literal name. One PASS/FAIL line per check. `python3 synth.py --selftest --model MODEL_ID` runs only the check; `--no-selftest` skips it (`--dry-run` makes no calls, so it never runs it). The call count appears in the summary line.

```sh
# 3. Arms: complete skillOverrides maps; each arm pins every skill explicitly. First --arm = baseline.
python3 make_arms.py --skills-dir "$HOME/.claude/skills" --out work/arms \
    --arm before --arm after --set 'after:jira-*=name-only' --set 'after:rarely-used=name-only'
# states: on | name-only | user-invocable-only | off. --set is ARM:GLOB=STATE, applied in order.
# To compare against your setup as it is today, start every arm from your settings:
#   --base-settings ~/.claude/settings.json --arm current --arm after --set 'after:x=name-only'

# 4. Run (resumable; re-run the same command after an interruption)
python3 run.py --cases work/cases.jsonl --arms work/arms --out work/results.jsonl --reps 2 --workers 3 --model MODEL_ID
tail -f work/results.jsonl.progress     # in another terminal
```
Each run is `claude -p <prompt> --model <id> --settings <arm>.json --tools Skill Read Grep Glob --strict-mcp-config --mcp-config '{"mcpServers":{}}' --max-turns 2 --output-format stream-json --verbose` in an empty temp directory (read-only tools, no MCP: side-effect free). It records the `Skill` tool_use names and the first assistant turn's `usage` (input + cache creation + cache read = start tokens). Results are append-only; keys are `arm|case|rep`; finished keys are skipped, errored keys are retried. Positives whose expected skills are all `user-invocable-only`/`off` in an arm are excluded by design (recorded, no call made). Skills are installed where Claude Code finds them (your user skills dir, or `.claude/skills` under `--cwd`).

```sh
# 5. Report
python3 report.py work/results.jsonl
```
Per arm: recall (all / implicit), wrong-skill rate, fire rate on negatives, median start tokens, each with a Wilson 95% CI. For every non-baseline arm: an exact two-sided McNemar test (binomial on discordant pairs) over per-case majority outcomes across reps, plus per-skill regressions. `--json` for machine output, `--baseline NAME` to change the baseline.

**Token savings.** For each case, `report.py` takes the median start tokens over that case's usable reps, per arm, and pairs each non-baseline arm with the baseline on cases where both have a value. The per-case ratio is arm/baseline. The report shows the geometric-mean ratio as a percent change, a 95% percentile bootstrap interval (cases resampled with replacement, 2000 resamples, seeded by `--seed`, default 0), and an exact two-sided sign test on cheaper vs costlier cases (ties excluded):

    slim start tokens vs base: -25.9% [-30.9, -16.0] (geo-mean, 95% bootstrap, 8 cases); cheaper on 7/8, sign p = 0.07

With fewer than 2 paired cases the line reads `n/a (k paired cases)`. The same numbers are under `savings.<arm>` in `--json` output.

## Quick version

`quick_ab.py` is the single-file check for hand-written prompts (`skill<TAB>prompt` per line) and two settings files:
```sh
python3 quick_ab.py --prompts prompts.tsv --a work/arms/before.json --b work/arms/after.json --reps 3 --model MODEL_ID
```
A run that exceeds `--deadline` or exits nonzero is left out of every count, shown as `(N err)` on its row and on an `errored runs:` summary line, and makes the exit code 2. A prompt with no ok runs in an arm is left out of McNemar. Progress lines are flushed, so you can tail a redirected output file while it runs.

## Cost notes

Runs = cases x arms x reps. The issue's reference run (98 cases, 2 arms, 2 reps) was 448 runs per round on Sonnet. A run is about 1 to 2 model turns with a start context of roughly 17k to 36k tokens depending on your setup, so budget accordingly; try `--limit 10 --reps 1` first. Filtering is 1 model call per 25 candidates. `--model` picks the model for every step; keep `--workers` low enough to stay under rate limits.

## Tests

```sh
python3 -m unittest discover -s ab/tests
```
Synthetic transcripts and a fake `claude` binary on `PATH`; no network or credits.

## Rename arms: does a better NAME help a name-only skill?

For a `name-only` skill the name is all the model sees. `make_rename_arms.py` builds an arm where chosen skills get new names (copies installed as project skills in a per-arm run dir, originals set to `off`); `run.py` runs that arm in its dir and maps fired names back, so `report.py` compares on the original names.

```sh
echo '{"boil-the-ocean": "going-all-in-or-about-to-claim-done"}' > renames.json
python3 make_rename_arms.py --skills-dir ~/.claude/skills --base arms/before.json --rename renames.json --name renamed --out arms
python3 run.py --arms arms/before.json arms/renamed.json --cases work/cases.jsonl --reps 3 --model MODEL_ID --out work/results.jsonl
```

Write the test prompts from the SKILL.md with a writer that never sees the new names, or the arm wins by construction. Measured once (7 opaque names, 35 fresh prompts x 3 reps): recall 49% -> 90%, McNemar p < 0.0001, other skills flat. A `use-this-skill-when-` prefix (`--prefix`) gained nothing over the same names and cost ~300 start tokens.

## Negative control arm

A round where nothing regresses might only mean the prompts were easy. A control arm proves the harness can see a loss. Setting skills to `off` does not work: those positives are excluded by design, so no calls are made. Instead every skill stays callable but loses its signal. It is renamed to an opaque `skill-NN` and set to `name-only`.

```sh
python3 make_rename_arms.py --skills-dir ~/.claude/skills --base arms/before.json --control --out arms
```

This writes `arms/control.json`, `arms/control.meta.json` (`"control": true`) and the renamed copies. `--name` changes the arm name. `run.py` runs it like any arm, maps fired names back, and stamps `control: true` on its records. `report.py` labels it `[control]`, skips the regression list for it, and prints a calibration line:

    control control: recall 0% vs baseline 100% (harness can detect loss: yes; McNemar p = 0.03125)

`yes` means control recall is below baseline and McNemar p < 0.05. `no` means the prompts or model find skills without the name, so "no loss" results in that round are weak evidence. `--json` adds `controls`, `calibration`, and `control: true` on the arm and paired entries. `report.py --control ARM` marks an arm as control for results recorded without the flag.

## Other clients: does a name index replace the native skill list? (`run_client.py`)

Question: if a CLI's native skill catalog drops most skills and its instructions file instead carries ONE "name index" paragraph, does the model still find and read the right skill? `run_client.py` builds three arms per client from Claude's `skillOverrides`:

- `listed`: every model-invocable skill in the client's native catalog (today's state).
- `index`: skills that are `name-only` in `skillOverrides` are removed from the catalog, and the instructions file gets one appended paragraph: `Other skills (not listed above). When one matches the task, read <store>/<name>/SKILL.md first: a, b, c.` `<store>` is `<arms-root>/store`, symlinks to every skill dir, outside the client's skill roots.
- `hidden`: the same removal with no paragraph. This is the control: `index` has to beat `hidden` to mean anything.

A run "fires" skill X when the agent's tool calls name a path ending in `/X/SKILL.md`. Only commands are scanned, never tool output, and globs are ignored. Start tokens are the input tokens of the first model request. A positive is excluded (recorded, no call) when none of its expected skills has a `SKILL.md` in the store, e.g. a dangling link. Records use `run.py`'s keys (`arm, case, rep, label, expected, skills, start_tokens, excluded, error`, ...), plus `client`, `session_id`, `read_paths`, `exit_status`. `report.py` reads them unchanged, and `listed` is the baseline.

```sh
R=/path/to/arms-codex
python3 run_client.py build --client codex --arms-root $R          # build + free check, no model call
python3 run_client.py probe --arms-root $R --arm listed --prompt "Read the SKILL.md of the skill named readback and summarize it."
python3 run_client.py run --arms-root $R --cases work/cases.jsonl --out work/codex-results.jsonl --reps 2
python3 report.py work/codex-results.jsonl --synthetic include
```

**codex adapter.** Each arm is its own `CODEX_HOME`. It holds a symlinked `auth.json` (credentials are never copied), the live `config.toml` with every `[mcp_servers...]` table removed, `AGENTS.md` (the live `AGENTS.override.md`, plus the paragraph in `index`), and `skills/` symlinks for the visible set. codex also scans `~/.agents/skills`, which sits outside `CODEX_HOME`, so hidden skills are also turned off with `[[skills.config]] name = "<n>" enabled = false`. codex leaves a skill out of its catalog when that skill's `agents/openai.yaml` sets `policy.allow_implicit_invocation: false`, and those skills stay out of every arm. Front-matter `disable-model-invocation` is ignored by codex. `build`/`check` render each arm with `codex debug prompt-input` (free) and print PASS/FAIL per arm: the catalog equals the plan, `AGENTS.md` is loaded, and the paragraph is present exactly once in `index` and absent elsewhere.

Runs go through ach, so they show up in `ach dash`: `bun ach.ts run --agent codex --wall-ms <deadline> --extra-args=--skip-git-repo-check --sandbox read-only --json <prompt>`. Each run gets a fresh empty cwd under `<arms-root>/cwd/`. `CODEX_HOME` passes through ach to codex because ach scrubs env only when a sandbox policy flag such as `--permission-mode` is given, and none is. Reads are detected from ach's `tool_call` events and from the arm's codex rollout (`<arm>/sessions/.../rollout-*<sessionId>.jsonl`, `custom_tool_call` / `function_call` items). Start tokens come from the rollout's first `token_count.last_token_usage.input_tokens`. ach's `tokens[0].inputTokens` covers the whole codex turn (every request, minus cache reads), so it is never used. Keep `--workers` at 1 or 2, because runs in one arm share that arm's sqlite state.

**agy adapter** (Google Antigravity CLI, `--client agy`). Each arm is its own `HOME`, because every root agy reads is HOME-relative: `~/.gemini/config/GEMINI.md` (and `AGENTS.md`, a symlink to it, deduped by agy), `~/.gemini/config/skills`, `~/.agents/skills`, and the app data under `~/.gemini/antigravity-cli`. agy keeps its OAuth token in the macOS login keychain and reaches it through `$HOME/Library/Keychains`, so each arm symlinks that directory (nothing is copied). An arm holds the keychain symlink, `antigravity-cli/settings.json` with only the live `model` plus `permissions.allow: ["read_file(*)"]`, `GEMINI.md` (the live one, plus the paragraph in `index`), and `.gemini/config/skills/<name>` symlinks for the visible set. There is no MCP config, no hooks.json and no `~/.agents`. agy leaves out a skill whose front matter sets `disable-model-invocation: true`. Headless print mode auto-denies every tool that needs approval, so without the read rule a SKILL.md read is refused and the run looks like a miss. Every other tool (shell, edits) stays denied, which keeps runs effectively read-only. `check` is free. It lists what agy will scan under the arm HOME, so the "+N system" figure counts the builtin skills agy installs, not the ones it lists. Passing `run --verify-prompt` records `prompt_catalog`, the `<skills>` block the model actually got, decoded from the run's conversation db (`antigravity-cli/conversations/<id>.db`, `gen_metadata`). Builtin skills show up there as `system`.

Runs call `agy -p <prompt> --output-format stream-json --print-timeout <deadline>s` directly (agy is not an ach agent), with HOME set to the arm and a subprocess wall-clock deadline of deadline + 30 s. Each model request is a `step_update` with `step_type: agent_response` and its own `usage`. Start tokens are the first of these (`input_tokens + cache_read_tokens`). The final `result.usage` is the whole-turn sum: on a two-request run it was 28287 + 2056 = 30343. Reads are detected from `step_type: tool` events' `tool_info.parameters` (for example `view_file {"AbsolutePath": ".../<name>/SKILL.md"}`). Tool outputs are not scanned. If a run reports `read_file` in `denied_actions` (agy then says SUCCESS with an empty response), it is recorded as an error, so a resumed run does it again instead of counting it as a miss. The raw stream is kept as `<cwd>/.agy-stream.jsonl`. Keep `--workers 1`, because every arm shares the one login keychain.

**Adding a client** (omp, ...): write a class with `discover()`, `build_arm(arm, root, plan, catalog)`, `check_arm(root, cwd)` and `run(prompt, root, cwd, opts)` (returns `read_skills, native_skills, start_tokens, error, session_id, read_paths`), then register it in `ADAPTERS`. `native_skills` is for clients that have a real skill-invocation event. An adapter can define `model_invocable(skill_dir)` when its catalog rule differs from codex's. Constructor keywords are matched by name, so an adapter only gets the CLI flags it accepts.

Tests: `ab/tests/test_run_client.py` (a fake `codex`, a fake ach-running `bun` and a fake `agy`; no network).
