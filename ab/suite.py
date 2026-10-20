#!/usr/bin/env python3
"""Run a whole trigger suite: build arms, split cases, plan calls, run arm by arm, report.

One command that wraps make_rename_arms.py, run.py and report.py for a workspace whose
.claude/skills (and optional .claude/commands) you want to test:

  python3 suite.py --workspace ~/scratch/my-project --cases cases.jsonl --out work/suite-1 \\
      --model MODEL_ID [--arm baseline,control] [--split splits.json] [--profiles profiles.json] \\
      [--reps 2] [--workers 2] [--limit N] [--deadline 180] [--refuse ~/dev/my-project] [--dry-run]

Arms (--arm, comma list, first = baseline; default baseline,control):
  baseline    {"skillOverrides": {}} run in the workspace itself
  control     opaque rename of every skill and command, name-only (make_rename_arms.py --control)
  control-on  opaque rename with descriptions still listed (--control --state on)
  desc-blank  names kept, every skill and command name-only, run in the workspace
  FILE.json   any arm file you built (make_arms.py / make_rename_arms.py); its .meta.json is copied
Control arms run in OUT/.../arms/<arm>.cwd, a copy of the workspace .claude; the workspace is
only read.

Cases: kit schema (label/skills, see run.py) or the suite schema (kind/expected/expected_any),
converted on the fly. Cases with "prior_turns" (multi-turn) are skipped and listed in the
manifest: run single-shot they would be false negatives.

Splits (--split FILE, optional): {"name": ["skill", ...]} or {"name": {"skills": [...],
"tags": [...]}}. A case goes to the first split whose skills meet its expected names or whose
tags meet its "tags"; everything else goes to "headline". Each split gets its own results and
report. Profiles (--profiles FILE, optional): {"name": {"claude_args": [...], "env": {...}}},
merged into every arm's meta; default is one profile "default" with nothing extra.

Output in --out: manifest.json (kit sha, workspace sha, cases sha256, counts, planned calls,
runner snapshot), progress.log, cases.<split>.jsonl, results-<split>.jsonl,
report-<split>.txt|json, run-<split>.log (under <profile>/ with more than one profile).

Bounds: per-call --deadline; after each arm the run stops (exit 4, "aborted" in the manifest)
when errors exceed --max-error-rate (default 0.2) of finished calls. Resume: re-run the same
command on the same --out; finished keys are skipped and errored keys retried (run.py).

Snapshot: run as a script, suite.py first copies the kit's ab/*.py into a private temp dir and
re-executes from there, so editing the kit mid-run cannot change a live run. The manifest
records the snapshot path and the sha256 of suite.py; the snapshot is removed on exit.
"""
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_ENV = "CTXDIET_SUITE_KIT"            # real ab/ dir when running from a snapshot
SNAPSHOT_ENV = "CTXDIET_SUITE_SNAPSHOT"  # snapshot dir; set means "already re-executed"
BUILTIN_ARMS = ("baseline", "control", "control-on", "desc-blank")
DEFAULT_SPLIT = "headline"
GIT_TIMEOUT = 20


# ---------- cases ----------

def expected_names(c):
    """Expected names of a case in either schema (kit: skills/expected_any; suite: expected/expected_any)."""
    names = list(c.get("expected_any") or [])
    exp = c.get("expected") or []
    for n in ([exp] if isinstance(exp, str) else list(exp)) + list(c.get("skills") or []):
        if n not in names:
            names.append(n)
    return [n for n in names if n.lower() != "none"]


def convert(c):
    """Suite schema (kind/expected) to kit schema (label/skills). Kit-schema cases pass through."""
    if c.get("label") in ("positive", "negative"):
        return dict(c)
    out = dict(c)
    exp = expected_names(c)
    if c.get("kind") == "negative" or not exp:
        out["label"] = "negative"
        out["skills"] = ["none"]
        out.pop("expected_any", None)
    else:
        out["label"] = "positive"
        out["skills"] = exp
        if c.get("expected_any"):
            out["expected_any"] = list(exp)
    return out


def load_splits(path):
    """Split config -> ordered [(name, skills set, tags set)]."""
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("split config must be a JSON object {name: [skills] | {skills, tags}}")
    out = []
    for name, spec in raw.items():
        if name == DEFAULT_SPLIT:
            raise ValueError("split name %r is reserved for unmatched cases" % DEFAULT_SPLIT)
        if isinstance(spec, list):
            spec = {"skills": spec}
        if not isinstance(spec, dict):
            raise ValueError("split %r: expected a list of skills or {skills, tags}" % name)
        out.append((name, {s.lower() for s in spec.get("skills") or []},
                    set(spec.get("tags") or [])))
    return out


def split_of(case, splits):
    exp = {n.lower() for n in expected_names(case)}
    tags = set(case.get("tags") or [])
    for name, skills, stags in splits:
        if (skills & exp) or (stags & tags):
            return name
    return DEFAULT_SPLIT


def split_cases(cases_path, splits, out_dir):
    """Write OUT/cases.<split>.jsonl for every split (headline first). Returns (counts, files)."""
    with open(cases_path, "rb") as fb:
        sha = hashlib.sha256(fb.read()).hexdigest()
    names = [DEFAULT_SPLIT] + [s[0] for s in splits]
    rows = {n: [] for n in names}
    skipped, total = [], 0
    with open(cases_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            c = json.loads(line)
            total += 1
            if c.get("prior_turns"):
                skipped.append(c["id"])
                continue
            rows[split_of(c, splits)].append(convert(c))
    files = {}
    for n in names:
        files[n] = os.path.join(out_dir, "cases.%s.jsonl" % n)
        with open(files[n], "w", encoding="utf-8") as fh:
            for r in rows[n]:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    counts = {"total": total, "splits": {n: len(rows[n]) for n in names},
              "skipped_multi_turn": skipped, "cases_sha256": sha}
    return counts, files


def plan_calls(split_counts, limit, reps, n_arms, n_profiles):
    """Upper bound on model calls (excluded positives make no call)."""
    per = sum(min(n, limit) if limit else n for n in split_counts.values())
    return per * reps * n_arms * n_profiles


def error_rate(results, arm):
    """(finished, errors) for ARM in a results file; excluded rows do not count. Counted per key,
    like run.py's resume: a key with any ok record is ok, so a retried error counts once."""
    ok = {}
    if os.path.exists(results):
        with open(results, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("arm") == arm and not r.get("excluded"):
                    ok[r.get("key")] = ok.get(r.get("key"), False) or not r.get("error")
    return len(ok), sum(1 for v in ok.values() if not v)


# ---------- manifest ----------

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def update_manifest(path, **kv):
    m = read_json(path, {})
    m.update(kv)
    write_json(path, m)
    return m


def git_sha(path):
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"


def file_sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ---------- arms ----------

def is_inside(path, root):
    p, r = os.path.realpath(path), os.path.realpath(os.path.expanduser(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def write_meta(path, profile, cwd):
    """Create or merge ARM.meta.json with the profile's claude_args/env and the run cwd."""
    m = read_json(path, {})
    m["cwd"] = cwd
    if profile.get("claude_args"):
        m["claude_args"] = list(m.get("claude_args") or []) + list(profile["claude_args"])
    if profile.get("env"):
        m["env"] = dict(m.get("env") or {}, **{str(k): str(v) for k, v in profile["env"].items()})
    write_json(path, m)


def name_only_overrides(claude_dir):
    """desc-blank: every skill and command listed by name only (no rename, no description)."""
    ov = {}
    sd = os.path.join(claude_dir, "skills")
    if os.path.isdir(sd):
        for n in sorted(os.listdir(sd)):
            if os.path.isfile(os.path.join(sd, n, "SKILL.md")):
                ov[n] = "name-only"
    cd = os.path.join(claude_dir, "commands")
    for root, _dirs, files in os.walk(cd):
        for f in sorted(files):
            if f.endswith(".md"):
                rel = os.path.relpath(os.path.join(root, f[:-3]), cd)
                ov[":".join(rel.split(os.sep))] = "name-only"
    return ov


def arm_name(spec):
    return spec if spec in BUILTIN_ARMS else os.path.splitext(os.path.basename(spec))[0]


def build_arms(arm_specs, arms_dir, ws, profile, refuse):
    import make_rename_arms
    os.makedirs(arms_dir, exist_ok=True)
    claude = os.path.join(ws, ".claude")
    base = os.path.join(arms_dir, "baseline.json")
    write_json(base, {"skillOverrides": {}})
    for spec in arm_specs:
        name = arm_name(spec)
        arm, meta = os.path.join(arms_dir, name + ".json"), os.path.join(arms_dir, name + ".meta.json")
        cwd = ws
        if spec in ("control", "control-on"):
            cwd = os.path.join(arms_dir, name + ".cwd")
            for r in refuse:
                if is_inside(cwd, r):
                    raise ValueError("refusing control dir %s: inside protected %s" % (cwd, r))
            shutil.rmtree(cwd, ignore_errors=True)
            shutil.copytree(claude, os.path.join(cwd, ".claude"), symlinks=True)
            for p in (arm, meta):
                if os.path.exists(p):
                    os.remove(p)
            args = ["--skills-dir", os.path.join(claude, "skills"), "--base", base, "--control",
                    "--name", name, "--out", arms_dir, "--cwd", cwd]
            if os.path.isdir(os.path.join(claude, "commands")):
                args += ["--commands-dir", os.path.join(claude, "commands")]
            if spec == "control-on":
                args += ["--state", "on"]
            with contextlib.redirect_stdout(sys.stderr):
                if make_rename_arms.main(args) != 0:
                    raise ValueError("make_rename_arms failed for arm %s" % name)
        elif spec == "desc-blank":
            write_json(arm, {"skillOverrides": name_only_overrides(claude)})
            if os.path.exists(meta):
                os.remove(meta)
        elif spec != "baseline":
            src_meta = spec[:-5] + ".meta.json"
            shutil.copyfile(spec, arm)
            if os.path.exists(meta):
                os.remove(meta)
            if os.path.exists(src_meta):
                shutil.copyfile(src_meta, meta)
                cwd = read_json(meta).get("cwd") or ws
        elif os.path.exists(meta):
            os.remove(meta)
        write_meta(meta, profile, cwd)


# ---------- main ----------

def parse_args(argv):
    import _claude
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", required=True, help="dir with .claude/skills (only read)")
    ap.add_argument("--cases", required=True, help="cases .jsonl (kit or suite schema)")
    ap.add_argument("--out", required=True, help="output dir; re-run on the same dir to resume")
    ap.add_argument("--model", required=True, type=_claude.model_arg, help="model id for the runs (required)")
    ap.add_argument("--arm", default="baseline,control",
                    help="comma list of %s or arm .json paths; first = baseline" % "|".join(BUILTIN_ARMS))
    ap.add_argument("--split", default=None, help="split config JSON (see above)")
    ap.add_argument("--profiles", default=None, help="profiles JSON {name: {claude_args, env}}")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="only the first N cases of each split")
    ap.add_argument("--deadline", type=float, default=180, help="per-call wall-clock seconds")
    ap.add_argument("--max-error-rate", type=float, default=0.2,
                    help="stop when errors exceed this share of an arm's finished calls (default 0.2)")
    ap.add_argument("--refuse", action="append", default=[], metavar="PATH",
                    help="refuse a workspace or output inside PATH (repeatable; e.g. your real checkout)")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--dry-run", action="store_true", help="build arms, split and plan; no model calls")
    return ap, ap.parse_args(argv)


def main(argv=None):
    ap, a = parse_args(argv)
    import report
    import run
    specs = [s.strip() for s in a.arm.split(",") if s.strip()]
    for s in specs:
        if s not in BUILTIN_ARMS and not (s.endswith(".json") and os.path.isfile(s)):
            ap.error("--arm %r: not one of %s and not an arm .json file" % (s, ", ".join(BUILTIN_ARMS)))
    names = [arm_name(s) for s in specs]
    if not names or len(set(names)) != len(names):
        ap.error("--arm needs at least one arm and unique arm names")
    if not os.path.isfile(a.cases):
        ap.error("cases file not found: %s" % a.cases)
    if not os.path.isdir(a.workspace):
        ap.error("workspace not found: %s" % a.workspace)
    ws = os.path.realpath(a.workspace)
    out = os.path.realpath(a.out)
    for r in a.refuse:
        for label, p in (("workspace", ws), ("output", out)):
            if is_inside(p, r):
                print("suite: refusing %s %s: inside protected %s" % (label, p, r), file=sys.stderr)
                return 3
    if is_inside(out, ws):
        print("suite: refusing output %s inside the workspace (it is only read)" % out, file=sys.stderr)
        return 3
    if not os.path.isdir(os.path.join(ws, ".claude", "skills")):
        ap.error("no .claude/skills in workspace %s" % ws)
    try:
        splits = load_splits(a.split)
        profiles = read_json(a.profiles) if a.profiles else {"default": {}}
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError("profiles must be a non-empty JSON object {name: {claude_args, env}}")
    except (OSError, ValueError) as e:
        ap.error(str(e))

    os.makedirs(out, exist_ok=True)
    log_path = os.path.join(out, "progress.log")

    def say(msg):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line, file=sys.stderr)

    counts, files = split_cases(a.cases, splits, out)
    say("cases: %s" % json.dumps(counts))
    planned = plan_calls(counts["splits"], a.limit, a.reps, len(names), len(profiles))
    kit = os.environ.get(KIT_ENV) or HERE
    mpath = os.path.join(out, "manifest.json")
    prev = read_json(mpath, {})
    if prev.get("case_counts", {}).get("cases_sha256") not in (None, counts["cases_sha256"]):
        say("warning: cases changed since the last run on this --out (sha256 differs)")
    m = {k: v for k, v in prev.items() if k not in ("aborted", "end_time")}
    m.update(kit=kit, kit_sha=git_sha(kit), workspace=ws, workspace_sha=git_sha(ws), model=a.model,
             arms=names, profiles=list(profiles), splits=list(counts["splits"]), case_counts=counts,
             reps=a.reps, limit=a.limit or None, workers=a.workers, deadline=a.deadline,
             max_error_rate=a.max_error_rate, dry_run=a.dry_run, planned_calls=planned,
             cases_file=os.path.realpath(a.cases), runner_snapshot=os.environ.get(SNAPSHOT_ENV),
             runner_sha256=file_sha(os.path.join(HERE, "suite.py")), start_time=now(),
             runs=prev.get("runs", 0) + 1)
    m.setdefault("first_start_time", m["start_time"])
    write_json(mpath, m)

    for pname, profile in profiles.items():
        pdir = os.path.join(out, pname) if len(profiles) > 1 else out
        arms_dir = os.path.join(pdir, "arms")
        try:
            build_arms(specs, arms_dir, ws, profile or {}, a.refuse)
        except (OSError, ValueError) as e:
            print("suite: %s" % e, file=sys.stderr)
            update_manifest(mpath, aborted="arm build failed: %s" % e, end_time=now())
            return 2
        with open(os.path.join(arms_dir, "order.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(names) + "\n")
        say("[%s] arms built: %s (planned calls overall: %d)" % (pname, ",".join(names), planned))
        if a.dry_run:
            continue
        for split, cases in files.items():
            if not counts["splits"][split]:
                say("[%s/%s] no cases, skipped" % (pname, split))
                continue
            res = os.path.join(pdir, "results-%s.jsonl" % split)
            for arm in names:
                say("[%s/%s] arm %s start" % (pname, split, arm))
                rargs = ["--cases", cases, "--arms", os.path.join(arms_dir, arm + ".json"), "--out", res,
                         "--reps", str(a.reps), "--model", a.model, "--deadline", str(a.deadline),
                         "--workers", str(a.workers), "--progress", os.path.join(pdir, "progress-%s.log" % split),
                         "--claude-bin", a.claude_bin]
                if a.limit:
                    rargs += ["--limit", str(a.limit)]
                with open(os.path.join(pdir, "run-%s.log" % split), "a", encoding="utf-8") as lf, \
                        contextlib.redirect_stderr(lf):
                    rc = run.main(rargs)
                fin, err = error_rate(res, arm)
                say("[%s/%s] arm %s done: finished=%d errors=%d run_rc=%d" % (pname, split, arm, fin, err, rc))
                if rc != 0 and fin == 0:
                    update_manifest(mpath, aborted="run.py failed for %s/%s/%s" % (pname, split, arm), end_time=now())
                    print("suite: run.py failed for %s/%s/%s, see run-%s.log" % (pname, split, arm, split),
                          file=sys.stderr)
                    return 5
                if fin and err > a.max_error_rate * fin:
                    why = "error rate over %g%% in %s/%s/%s (%d/%d)" % (
                        a.max_error_rate * 100, pname, split, arm, err, fin)
                    update_manifest(mpath, aborted=why, end_time=now())
                    say("stopping: " + why)
                    return 4
            for fmt, ext in (([], "txt"), (["--json"], "json")):
                with open(os.path.join(pdir, "report-%s.%s" % (split, ext)), "w", encoding="utf-8") as fh, \
                        contextlib.redirect_stdout(fh):
                    try:
                        report.main([res, "--baseline", names[0]] + fmt)
                    except Exception as e:  # noqa: BLE001 - a report failure must not lose the run
                        say("report %s.%s failed: %s" % (split, ext, e))
    if a.dry_run:
        print("dry-run: planned model calls = %d" % planned)
    update_manifest(mpath, end_time=now())
    say("done: %s" % out)
    return 0


def snapshot_and_exec(argv):
    """Copy the kit's ab/*.py to a private temp dir and re-exec suite.py from there."""
    snap = tempfile.mkdtemp(prefix="ctxdiet-suite-")
    for f in os.listdir(HERE):
        if f.endswith(".py"):
            shutil.copy2(os.path.join(HERE, f), os.path.join(snap, f))
    env = dict(os.environ, **{KIT_ENV: HERE, SNAPSHOT_ENV: snap})
    try:
        os.execve(sys.executable, [sys.executable, os.path.join(snap, "suite.py")] + list(argv), env)
    except OSError:
        shutil.rmtree(snap, ignore_errors=True)
        raise


def run_from_snapshot(argv):
    snap = os.environ.get(SNAPSHOT_ENV)
    try:
        return main(argv)
    finally:
        # only ever remove the dir we are running from, and only when it is the snapshot
        if snap and os.path.realpath(snap) == os.path.realpath(HERE):
            shutil.rmtree(snap, ignore_errors=True)


if __name__ == "__main__":
    if not os.environ.get(SNAPSHOT_ENV):
        snapshot_and_exec(sys.argv[1:])
    sys.exit(run_from_snapshot(sys.argv[1:]))
