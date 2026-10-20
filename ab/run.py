#!/usr/bin/env python3
"""Run each case under each arm N times and record which Skill(s) fired.

Per run:  claude -p PROMPT --model MODEL --settings ARM.json --tools Skill Read Grep Glob
          --strict-mcp-config --mcp-config '{"mcpServers":{}}' --max-turns 2
          --output-format stream-json --verbose
Read-only tools and no MCP keep runs side-effect free.

Results are append-only JSONL, one record per (arm, case, rep). Re-running the
same command skips finished keys (errors are retried unless --keep-errors).
A tailable progress log goes to --progress (default: <out>.progress).
Positives whose expected skills are all `user-invocable-only`/`off` in an arm are
excluded by design (recorded with excluded=true, no call made).

Rename arms (make_rename_arms.py) add ARM.meta.json next to ARM.json:
{"cwd": dir whose .claude/skills holds renamed copies, "rename": {new: old}}.
Runs use that cwd, and fired names are mapped back to the original names.
"control": true in the meta (make_rename_arms.py --control) marks a negative-control
arm; its records carry control=true so report.py can label it.

Per-arm CLI isolation: ARM.meta.json may also carry "claude_args" (extra `claude` args appended
to the call, e.g. --setting-sources project,local --strict-mcp-config --mcp-config <json>; a
--mcp-config / --strict-mcp-config given there replaces the default) and "env" (extra child
environment variables, e.g. CLAUDE_CONFIG_DIR).

Matching is exact on the full namespaced name ("a:b:c" matches only "a:b:c"; "context",
"team:context" and "team:dev:context" are three different skills). A case may set
"expected_any": [names] (any one is acceptable); "none" in the expected list means
"no skill fired" is acceptable (negatives).

Each record also has "listing" (listing_chars, listing_entries, name_only, with_description,
parsed from the session transcript's skill_listing attachment; null if unavailable) and
"fired_via": {name: "skill" | "read"}. "skills" lists Skill-tool fires only (the primary
signal); "read_skills" lists skills/commands whose SKILL.md / command .md was Read (full namespaced
name from the path: .claude/skills or plugin skills/<name>, .claude/commands or plugin commands/a/b.md -> a:b).
"expected" is the sorted acceptable names minus "none"; "expect_none" is true for negatives. Provider/API errors are
kept as rows with error, is_error=true and error_class (rate_limit, overloaded, auth,
timeout, no_output, api_error).

Billing: strips ANTHROPIC_API_KEY from the child env so the subscription is used
(--keep-api-key to disable). Each call has a wall-clock --deadline.
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import sys
import tempfile
import threading
import time

import _claude

DEFAULT_TOOLS = ["Skill", "Read", "Grep", "Glob"]


SKILL_PATH_RE = re.compile(r"(?:^|/)skills/([^/\s]+)/SKILL\.md$")
COMMAND_PATH_RE = re.compile(r"(?:(?:^|/)\.claude/|/plugins/(?:[^\s]*/)?)commands/(.+)\.md$")


def read_fire_name(path):
    """Full namespaced name a Read of `path` counts as a fire of, or None. Anchored to real roots:
    .../.claude/skills/<name>/SKILL.md or plugin .../skills/<name>/SKILL.md -> <name>;
    .../.claude/commands/a/b/c.md or plugin .../commands/a/b/c.md -> a:b:c.
    Other paths (docs/commands/x.md, docs/skills/a/b/SKILL.md) do not count."""
    if not path or "*" in path:
        return None
    m = SKILL_PATH_RE.search(path)
    if m and m.group(1) != "<name>":
        return m.group(1)
    m = COMMAND_PATH_RE.search(path)
    if m:
        return ":".join(x for x in m.group(1).split("/") if x) or None
    return None


def classify_error(text):
    t = (text or "").lower()

    def code(c):
        return re.search(r"\b%s\b" % c, t)
    if code("429") or "rate limit" in t or "rate_limit" in t:
        return "rate_limit"
    if code("529") or "overloaded" in t:
        return "overloaded"
    if code("401") or code("403") or "api key" in t or "authentication" in t or "unauthorized" in t:
        return "auth"
    if "deadline" in t or "timed out" in t or "timeout" in t:
        return "timeout"
    if "no usable stream output" in t:
        return "no_output"
    return "api_error"


def parse_stream(text):
    """Parse stream-json output -> dict(skills, read_skills, start_tokens, result_ok, num_turns,
    session_id, result_error). skills = Skill tool calls; read_skills = Reads of SKILL.md/command files."""
    skills, reads, first_usage, result, sid = [], [], None, None, None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        sid = ev.get("session_id") or sid
        t = ev.get("type")
        if t == "assistant":
            msg = ev.get("message") or {}
            if first_usage is None and msg.get("usage"):
                u = msg["usage"]
                first_usage = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                               + u.get("cache_read_input_tokens", 0))
            for b in msg.get("content") or []:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                inp = b.get("input") or {}
                if b.get("name") == "Skill":
                    s = inp.get("skill")
                    if s:
                        skills.append(s)
                elif b.get("name") == "Read":
                    n = read_fire_name(inp.get("file_path") or "")
                    if n and n not in reads:
                        reads.append(n)
        elif t == "result":
            result = ev
    err = None
    if result and result.get("is_error") and result.get("subtype") != "error_max_turns":
        err = str(result.get("result") or result.get("subtype") or "error")
    return {"skills": skills, "read_skills": reads, "start_tokens": first_usage, "session_id": sid,
            "result_ok": bool(result) and not result.get("is_error"), "result_error": err,
            "num_turns": (result or {}).get("num_turns")}


def skill_matches(fired, expected):
    """Exact match on the full namespaced name (case-insensitive): "a:b:c" matches only "a:b:c"."""
    return fired.lower() == expected.lower()


def expected_set(case):
    """Acceptable names for a case: `expected_any` if present, else `skills`. "none" means no fire is acceptable."""
    return list(case.get("expected_any") or case.get("skills") or [])


def expectation_met(fired, case):
    exp = expected_set(case)
    if any(e.lower() == "none" for e in exp) and not fired:
        return True
    return any(skill_matches(f, e) for f in fired for e in exp if e.lower() != "none")


def build_args(prompt, arm_path, model=None, max_turns=2, tools=None, extra_args=None):
    extra = list(extra_args or [])
    args = ["-p", prompt, "--settings", arm_path, "--tools"] + list(tools or DEFAULT_TOOLS)
    if "--strict-mcp-config" not in extra and not any(x.startswith("--strict-mcp-config=") for x in extra):
        args.append("--strict-mcp-config")
    if "--mcp-config" not in extra and not any(x.startswith("--mcp-config=") for x in extra):
        args += ["--mcp-config", '{"mcpServers":{}}']
    args += ["--max-turns", str(max_turns), "--output-format", "stream-json", "--verbose"]
    if model:
        args += ["--model", model]
    return args + extra


def arm_state(arm_path, skill):
    with open(arm_path, encoding="utf-8") as fh:
        ov = json.load(fh).get("skillOverrides", {})
    for k, v in ov.items():
        if k.lower() == skill.lower():
            return v
    return "on"


def read_listing(session_id, env_extra=None):
    """Parse the skill_listing attachment(s) of a session transcript, or None if unavailable.
    Looks in $CLAUDE_CONFIG_DIR (or ~/.claude)/projects/*/<session_id>.jsonl."""
    if not session_id:
        return None
    try:
        base = (env_extra or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") \
            or os.path.expanduser("~/.claude")
        paths = glob.glob(os.path.join(glob.escape(base), "projects", "*", glob.escape(session_id) + ".jsonl"))
        if not paths:
            return None
        chars, name_only, with_desc, found = 0, [], [], False
        with open(paths[0], encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                att = ev.get("attachment") if isinstance(ev, dict) else None
                if not isinstance(att, dict) or att.get("type") != "skill_listing":
                    continue
                content = att.get("content")
                if not isinstance(content, str):
                    continue
                found = True
                chars += len(content)
                for ln in content.splitlines():
                    if not ln.startswith("- "):
                        continue
                    name, sep, _desc = ln[2:].partition(": ")
                    name = name.strip()
                    if name:
                        (with_desc if sep else name_only).append(name)
        if not found:
            return None
        return {"listing_chars": chars, "listing_entries": len(name_only) + len(with_desc),
                "name_only": name_only, "with_description": with_desc}
    except Exception:  # noqa: BLE001 - listing capture must never break a run
        return None


def load_meta(arm_path):
    """Optional ARM.meta.json beside ARM.json (rename arms): {"cwd": ..., "rename": {new: old}}."""
    mp = arm_path[:-5] + ".meta.json" if arm_path.endswith(".json") else arm_path + ".meta.json"
    if not os.path.exists(mp):
        return {}
    with open(mp) as fh:
        return json.load(fh)


def unrename(skills, meta):
    """Map fired (possibly renamed, possibly plugin-prefixed) names back to original names."""
    ren = dict(meta.get("rename") or {})
    # command renames are full namespaced names ({old full: new full}); exact match wins
    ren.update({new: old for old, new in (meta.get("command_rename") or {}).items()})
    return [ren.get(s, s) for s in skills]


def is_excluded(case, arm_path):
    exp = [e for e in expected_set(case) if e.lower() != "none"]
    meta = load_meta(arm_path)
    fwd = {old: new for new, old in (meta.get("rename") or {}).items()}
    fwd.update(meta.get("command_rename") or {})
    return (case.get("label") == "positive" and exp
            and all(arm_state(arm_path, fwd.get(s, s)) in ("user-invocable-only", "off") for s in exp))


def done_keys(out_path, keep_errors):
    keys = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if keep_errors or not r.get("error"):
                    keys.add(r["key"])
    return keys


def run_one(case, arm, arm_path, rep, opts, arm_idx=0):
    rec = {"key": "%s|%s|%d" % (arm, case["id"], rep), "arm": arm, "arm_idx": arm_idx, "case": case["id"], "rep": rep,
           "label": case["label"],
           "expected": sorted(e for e in expected_set(case) if e.lower() != "none"),
           "explicit": bool(case.get("explicit")), "synthetic": bool(case.get("synthetic")), "prompt": case["prompt"][:200],
           "ts": int(time.time())}
    if case.get("label") == "negative" or any(e.lower() == "none" for e in expected_set(case)):
        rec["expect_none"] = True
    if is_excluded(case, arm_path):
        rec.update(excluded=True, skills=[], start_tokens=None)
        return rec
    t0 = time.time()
    meta = load_meta(arm_path)
    if meta.get("control"):
        rec["control"] = True
    if case.get("expected_any"):
        rec["expected_any"] = list(case["expected_any"])
    arm_env = {str(k): str(v) for k, v in (meta.get("env") or {}).items()}
    try:
        out, _rc = _claude.run_claude(
            build_args(case["prompt"], arm_path, opts.model, opts.max_turns, opts.tools, meta.get("claude_args")),
            deadline=opts.deadline, cwd=meta.get("cwd") or opts.cwd, strip_api_key=not opts.keep_api_key,
            extra_env=arm_env or None, claude_bin=opts.claude_bin)
        p = parse_stream(out)
        if not p["result_ok"] and not p["skills"] and not p["read_skills"] and p["start_tokens"] is None \
                and not p["result_error"]:
            raise _claude.ClaudeError("no usable stream output: %r" % out[:200])
        fired = unrename(p["skills"], meta)
        reads = [s for s in dict.fromkeys(unrename(p["read_skills"], meta))]
        via = {s: "skill" for s in fired}
        for s in reads:
            via.setdefault(s, "read")
        rec.update(skills=fired, read_skills=reads, fired_via=via, start_tokens=p["start_tokens"], num_turns=p["num_turns"],
                   session_id=p["session_id"], listing=read_listing(p["session_id"], arm_env))
        if p["result_error"]:
            rec.update(error=p["result_error"][:300], is_error=True, error_class=classify_error(p["result_error"]))
    except Exception as e:  # noqa: BLE001 - record and continue
        rec.update(error=str(e)[:300], is_error=True, error_class=classify_error(str(e)), skills=[],
                   start_tokens=None, listing=None)
    rec["secs"] = round(time.time() - t0, 1)
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", required=True, help="kept cases .jsonl (from filter.py)")
    ap.add_argument("--arms", required=True, nargs="+", help="arm .json files, or a make_arms.py output dir (order.txt gives order); first = baseline")
    ap.add_argument("--out", required=True, help="append-only results.jsonl")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--model", required=True, type=_claude.model_arg, help="model id for the runs (required)")
    ap.add_argument("--max-turns", type=int, default=2)
    ap.add_argument("--tools", nargs="+", default=DEFAULT_TOOLS)
    ap.add_argument("--deadline", type=float, default=180, help="per-call wall-clock seconds")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0, help="only the first N cases")
    ap.add_argument("--cwd", default=None, help="working dir for the runs (default: fresh empty temp dir)")
    ap.add_argument("--progress", default=None)
    ap.add_argument("--keep-errors", action="store_true", help="treat errored keys as done")
    ap.add_argument("--keep-api-key", action="store_true")
    ap.add_argument("--claude-bin", default="claude")
    a = ap.parse_args(argv)

    arm_files = []
    for p in a.arms:
        if os.path.isdir(p):
            names = sorted(f[:-5] for f in os.listdir(p) if f.endswith(".json") and not f.endswith(".meta.json"))
            order = os.path.join(p, "order.txt")  # written by make_arms.py
            if os.path.exists(order):
                with open(order, encoding="utf-8") as fh:
                    listed = [x.strip() for x in fh if x.strip()]
                names = [n for n in listed if n in names] + [n for n in names if n not in listed]
            arm_files += [os.path.join(p, n + ".json") for n in names]
        else:
            arm_files.append(p)
    arms = [(os.path.splitext(os.path.basename(p))[0], os.path.abspath(p)) for p in arm_files]
    cases = _claude.read_jsonl(a.cases)
    if a.limit:
        cases = cases[:a.limit]
    a.cwd = a.cwd or tempfile.mkdtemp(prefix="ctxdiet-ab-")
    done = done_keys(a.out, a.keep_errors)
    todo = [(c, arm, path, rep, a, idx) for rep in range(a.reps) for c in cases
            for idx, (arm, path) in enumerate(arms)
            if "%s|%s|%d" % (arm, c["id"], rep) not in done]
    prog_path = a.progress or a.out + ".progress"
    print("%d runs to do (%d already done)" % (len(todo), len(done)), file=sys.stderr)
    lock, n_done, n_err = threading.Lock(), 0, 0

    def work(item):
        nonlocal n_done, n_err
        rec = run_one(*item)
        with lock:
            with open(a.out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_done += 1
            n_err += 1 if rec.get("error") else 0
            line = "[%d/%d] %s %s fired=%s%s" % (n_done, len(todo), rec["key"], rec["expected"],
                                                 rec["skills"], " ERROR" if rec.get("error") else "")
            with open(prog_path, "a", encoding="utf-8") as pf:
                pf.write(line + "\n")
            print(line, file=sys.stderr)

    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        list(ex.map(work, todo))
    print("done: %d runs, %d errors -> %s" % (n_done, n_err, a.out), file=sys.stderr)
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
