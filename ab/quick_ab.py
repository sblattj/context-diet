#!/usr/bin/env python3
"""Single-file quick trigger A/B: does a Skill fire, before vs. after a settings change?

For the full real-prompt pipeline use mine/filter/make_arms/run/report. This is the
quick version: hand-written plain-language prompts, two settings files, count Skill hits.

Prompts file: one per line, `skill-name<TAB>prompt` (blank lines and # comments ignored).
Example:
  quick_ab.py --prompts prompts.tsv --a before.json --b after.json --reps 3 --model MODEL_ID
Prints hits per arm and the exact McNemar p over per-prompt majority outcomes.
Runs that error (deadline exceeded, nonzero exit) are excluded from every count and
denominator, reported per arm on an `errored runs:` line, and make the exit code 2
(0 = clean, no errored runs). Progress lines are flushed so a tailed output file is live.
Reuses run.py (stream parsing, deadline, API-key stripping) and report.py (stats).
"""
import argparse
import sys
import tempfile

import _claude
import run
from report import mcnemar_exact, wilson


def read_prompts(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            skill, _, prompt = line.partition("\t")
            if not prompt:
                raise SystemExit("bad line (need skill<TAB>prompt): %r" % line)
            rows.append((skill.strip(), prompt.strip()))
    return rows


def fired(prompt, settings, a):
    out, _ = _claude.run_claude(run.build_args(prompt, settings, a.model, a.max_turns),
                                deadline=a.deadline, cwd=a.cwd, strip_api_key=not a.keep_api_key,
                                claude_bin=a.claude_bin)
    return run.parse_stream(out)["skills"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompts", required=True)
    ap.add_argument("--a", required=True, help="settings.json for arm A (baseline)")
    ap.add_argument("--b", required=True, help="settings.json for arm B")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--model", required=True, type=_claude.model_arg, help="model id for the runs (required)")
    ap.add_argument("--max-turns", type=int, default=2)
    ap.add_argument("--deadline", type=float, default=180,
                    help="per-run seconds; a run that exceeds it (or exits nonzero) is excluded from the counts, "
                         "reported as errored, and makes the exit code 2")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--keep-api-key", action="store_true")
    ap.add_argument("--claude-bin", default="claude")
    a = ap.parse_args(argv)
    a.cwd = a.cwd or tempfile.mkdtemp(prefix="ctxdiet-quick-")
    maj, total, ok, err = {"A": [], "B": []}, {"A": 0, "B": 0}, {"A": 0, "B": 0}, {"A": 0, "B": 0}
    for skill, prompt in read_prompts(a.prompts):
        for tag, path in (("A", a.a), ("B", a.b)):
            hits = oks = errs = 0
            for _ in range(a.reps):
                try:
                    hits += any(run.skill_matches(f, skill) for f in fired(prompt, path, a))
                    oks += 1
                except _claude.ClaudeError as e:
                    errs += 1
                    print("error: %s" % e, file=sys.stderr, flush=True)
            total[tag] += hits
            ok[tag] += oks
            err[tag] += errs
            maj[tag].append(hits * 2 > oks if oks else None)  # None: no ok runs, left out of McNemar
            note = " (%d err)" % errs if errs else ""
            if not oks:
                note += " (no ok runs, left out of McNemar)"
            print("%s %-24s %d/%d%s  %s" % (tag, skill, hits, oks, note, prompt[:50]), flush=True)
    for tag in "AB":
        ci = "Wilson95 %.0f-%.0f%%" % tuple(100 * v for v in wilson(total[tag], ok[tag])) if ok[tag] else "Wilson95 n/a"
        print("arm %s: %d/%d runs hit (%s)" % (tag, total[tag], ok[tag], ci), flush=True)
    pairs = [(x, y) for x, y in zip(maj["A"], maj["B"]) if x is not None and y is not None]
    b = sum(1 for x, y in pairs if x and not y)
    c = sum(1 for x, y in pairs if y and not x)
    print("only A hit: %d, only B hit: %d, exact McNemar p = %.4g" % (b, c, mcnemar_exact(b, c)), flush=True)
    if len(pairs) < len(maj["A"]):
        print("prompts left out of McNemar (an arm had no ok runs): %d" % (len(maj["A"]) - len(pairs)), flush=True)
    if err["A"] or err["B"]:
        print("errored runs: A %d, B %d (excluded from the counts above)" % (err["A"], err["B"]), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
