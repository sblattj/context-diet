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
