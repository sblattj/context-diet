#!/usr/bin/env python3
"""Generate synthetic positive cases for skills with too few mined positives.

The issue's run added 14 synthetic positives on top of 45 mined ones. This step
automates that: for each skill in --skills-dir with fewer than --min-positives
positives in cases.jsonl, ask the --model you name (required, via the same
_claude.py call path as filter.py: deadline, bounded output, API key stripped)
for --per-skill realistic plain-language prompts that should trigger the skill
WITHOUT naming it. Each is appended to cases.jsonl as
{"id","prompt","label":"positive","skills":[name],"explicit":false,"synthetic":true}.

Skipped: skills that are user-invocable-only/off in any --arms file, skills with
`disable-model-invocation: true` frontmatter, and prompts duplicating an existing case.
report.py excludes synthetic cases by default so real recall is not inflated.
"""
import argparse
import json
import os
import re
import sys

import _claude
import make_arms
from filter import parse_ids, result_event
from mine import is_explicit
from run import arm_state

PROMPT = """Skill: %(name)s
Description: %(desc)s

Write %(n)d different realistic messages a user might type to a coding assistant that
this skill should handle. Plain everyday language, one or two sentences each, concrete
details (file names, topics), varied phrasing. Do NOT mention the skill name or say
"skill". Return ONLY a JSON array of %(n)d strings. No prose."""


BODY_CHAR_LIMIT = 4000

BODY_PROMPT = """Skill: %(name)s

The full text of this skill (instructions only) follows between the markers.
<<<SKILL
%(body)s
SKILL>>>

Write %(n)d different realistic messages a user might type to a coding assistant that
need this skill. Plain everyday language, one or two sentences each, concrete details
(file names, topics), varied phrasing. Do NOT name the skill, say "skill", or use a
slash command.
%(neg)s"""

NEG_TEXT = """Also write %(k)d near-miss messages: they sound related and share vocabulary, but
should NOT trigger this skill because the real need is something else. Same style.
Return ONLY a JSON object {"positives": [%(n)d strings], "negatives": [%(k)d strings]}. No prose."""
POS_ONLY_TEXT = "Return ONLY a JSON array of %(n)d strings. No prose."


def strip_frontmatter(text):
    m = re.match(r"---\s*\n.*?\n---[ \t]*\n?", text, re.S)
    return text[m.end():] if m else text


def read_skill_full(path_dir):
    """Return {name: {"desc", "body", "no_model"}} for skills under a dir (nested command trees included)."""
    out = {}
    for fallback, f in make_arms.iter_skills(path_dir):
        with open(f, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        m = re.match(r"---\s*\n(.*?)\n---", text[:6000], re.S)
        fm = m.group(1) if m else ""
        d = re.search(r"^description:\s*(.+?)(?=^\S|\Z)", fm, re.S | re.M)
        desc = re.sub(r"\s+", " ", d.group(1)).strip(" '\"") if d else ""
        out[make_arms.skill_name(f, fallback)] = {
            "desc": desc[:600], "body": strip_frontmatter(text).strip(),
            "no_model": bool(re.search(r"^disable-model-invocation:\s*true\s*$", fm, re.M))}
    return out


def read_skill(path_dir):
    """Return {name: description} for skills under a dir, plus the set that disable model invocation."""
    full = read_skill_full(path_dir)
    return ({n: v["desc"] for n, v in full.items()},
            {n for n, v in full.items() if v["no_model"]})


def parse_body_reply(text, n, k):
    """Return (positives, negatives) from an object reply, or (array, []) from a plain array."""
    m = re.search(r"\{.*\}", text, re.S)
    if k:
        if not m:
            raise ValueError("negatives requested but reply has no JSON object: %r" % text[:200])
        obj = json.loads(m.group(0))

        def clean(a, c):
            if not isinstance(a, list):
                raise ValueError("expected a JSON list, got %r" % (a,))
            return [s.strip() for s in a if isinstance(s, str) and s.strip()][:c]
        return clean(obj.get("positives"), n), clean(obj.get("negatives"), k)
    return parse_prompts(text, n), []


def parse_prompts(text, n):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in reply: %r" % text[:200])
    arr = json.loads(m.group(0))
    return [s.strip() for s in arr if isinstance(s, str) and s.strip()][:n]


def norm(s):
    return re.sub(r"\W+", " ", s.lower()).strip()


def call_generator(prompt, model, deadline, strip_api_key, claude_bin, max_output_tokens):
    args = ["-p", prompt, "--model", model, "--tools", "",
            "--output-format", "json", "--max-turns", "1", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    out, _ = _claude.run_claude(args, deadline=deadline, strip_api_key=strip_api_key, claude_bin=claude_bin,
                                extra_env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens)})
    env = result_event(json.loads(out))
    if env.get("is_error"):
        raise _claude.ClaudeError("generator error: %s" % str(env.get("result"))[:200])
    return env.get("result", "")


def generate(name, desc, n, model, deadline, strip_api_key=True, claude_bin="claude", max_output_tokens=1500):
    reply = call_generator(PROMPT % {"name": name, "desc": desc, "n": n}, model, deadline, strip_api_key,
                           claude_bin, max_output_tokens)
    return parse_prompts(reply, n)


def generate_from_body(name, body, n, model, deadline, negatives=0, strip_api_key=True, claude_bin="claude",
                       max_output_tokens=1500):
    """Body-based generation: the prompt sees the skill body only, never description/when_to_use.

    Returns (positives, negatives)."""
    tail = (NEG_TEXT % {"n": n, "k": negatives}) if negatives else (POS_ONLY_TEXT % {"n": n})
    prompt = BODY_PROMPT % {"name": name, "body": body[:BODY_CHAR_LIMIT], "n": n, "neg": tail}
    reply = call_generator(prompt, model, deadline, strip_api_key, claude_bin,
                           max_output_tokens * (2 if negatives else 1))
    return parse_body_reply(reply, n, negatives)


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "selftest", "synth.json")


def selftest(model, deadline, strip_api_key=True, claude_bin="claude", out=None):
    """Generate for the committed fixture skill; print one PASS/FAIL line per check.

    Returns (ok, calls). Checks: parses, requested count, non-empty, skill name absent."""
    out = out or sys.stderr
    with open(FIXTURE, encoding="utf-8") as fh:
        fx = json.load(fh)
    n, name = fx["n"], fx["name"]
    try:
        prompts = generate(name, fx["description"], n, model, deadline, strip_api_key, claude_bin)
    except (_claude.ClaudeError, ValueError, json.JSONDecodeError) as e:
        print("selftest FAIL generate + parse_prompts accepted the reply: %s" % e, file=out)
        return False, 1
    checks = [
        ("generate + parse_prompts accepted the reply", True),
        ("returned %d prompts (got %d)" % (n, len(prompts)), len(prompts) == n),
        ("every prompt non-empty", all(p.strip() for p in prompts)),
        ("no prompt contains the skill name %r" % name, not any(name.lower() in p.lower() for p in prompts)),
    ]
    for label, good in checks:
        print("selftest %s %s" % ("PASS" if good else "FAIL", label), file=out)
    return all(g for _, g in checks), 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="?", help="cases.jsonl (from filter.py); synthetic cases are appended to it")
    ap.add_argument("--skills-dir")
    ap.add_argument("--arms", nargs="*", default=[], help="arm .json files; skills hidden from the model in any are skipped")
    ap.add_argument("--min-positives", type=int, default=1, help="generate for skills with fewer positives than this (default 1 = none mined)")
    ap.add_argument("--per-skill", type=int, default=2, help="prompts per skill (the issue gives only a total of 14)")
    ap.add_argument("--model", required=True, type=_claude.model_arg, help="generator model id (required)")
    ap.add_argument("--source", choices=("description", "body"), default="description",
                    help="what the generator sees: the frontmatter description (default) or the skill body "
                         "(never the description, so later description cuts are not tested circularly)")
    ap.add_argument("--negatives", type=int, default=0,
                    help="with --source body: also generate N near-miss negatives per skill")
    ap.add_argument("--deadline", type=float, default=120)
    ap.add_argument("--keep-api-key", action="store_true")
    ap.add_argument("--claude-bin", default="claude")
    ap.add_argument("--dry-run", action="store_true", help="list the skills that would get prompts, call nothing")
    ap.add_argument("--selftest", action="store_true",
                    help="only generate for the committed fixture skill and check the output; exit 0 on pass, 1 on fail")
    ap.add_argument("--no-selftest", action="store_true",
                    help="skip the selftest that otherwise runs before a normal run")
    a = ap.parse_args(argv)

    if a.selftest:
        ok, _ = selftest(a.model, a.deadline, not a.keep_api_key, a.claude_bin)
        return 0 if ok else 1
    if a.negatives < 0:
        ap.error("--negatives must be >= 0")
    if not a.cases or not a.skills_dir:
        ap.error("cases and --skills-dir are required (unless --selftest)")
    st_calls = 0
    if not a.no_selftest and not a.dry_run:
        ok, st_calls = selftest(a.model, a.deadline, not a.keep_api_key, a.claude_bin)
        if not ok:
            print("ABORT: generator selftest failed for model %r; nothing was generated. "
                  "Fix the model or pass --no-selftest to skip." % a.model, file=sys.stderr)
            return 1

    cases = _claude.read_jsonl(a.cases)
    counts = {}
    for c in cases:
        if c.get("label") == "positive":
            for s in c.get("skills", []):
                counts[s] = counts.get(s, 0) + 1
    seen = {norm(c["prompt"]) for c in cases}
    full = read_skill_full(a.skills_dir)
    info = {n: v["desc"] for n, v in full.items()}
    no_model = {n for n, v in full.items() if v["no_model"]}
    body_mode = a.source == "body"
    if a.negatives and not body_mode:
        ap.error("--negatives requires --source body")
    added, failed = [], 0
    for name, desc in info.items():
        if counts.get(name, 0) >= a.min_positives:
            continue
        if name in no_model or any(arm_state(p, name) in ("user-invocable-only", "off") for p in a.arms):
            print("skip %s: not model-invocable" % name, file=sys.stderr)
            continue
        if a.dry_run:
            print("would generate %d for %s (%d mined)" % (a.per_skill, name, counts.get(name, 0)), file=sys.stderr)
            continue
        try:
            if body_mode:
                prompts, negs = generate_from_body(name, full[name]["body"], a.per_skill, a.model, a.deadline,
                                                   a.negatives, not a.keep_api_key, a.claude_bin)
            else:
                prompts, negs = generate(name, desc, a.per_skill, a.model, a.deadline, not a.keep_api_key,
                                         a.claude_bin), []
        except (_claude.ClaudeError, ValueError) as e:
            print("generation for %s failed: %s" % (name, e), file=sys.stderr)
            failed += 1
            continue
        k = 0
        for p in prompts:
            if norm(p) in seen or is_explicit(p, name):
                continue
            seen.add(norm(p))
            row = {"id": "syn-%s-%d" % (name.replace(":", "_"), k), "prompt": p, "label": "positive",
                   "skills": [name], "explicit": False, "synthetic": True}
            if body_mode:
                row["source"] = "synth-body"
            added.append(row)
            k += 1
        j = 0
        for p in negs:
            if norm(p) in seen or is_explicit(p, name):
                continue
            seen.add(norm(p))
            added.append({"id": "syn-neg-%s-%d" % (name.replace(":", "_"), j), "prompt": p, "label": "negative",
                          "skills": [], "explicit": False, "synthetic": True, "source": "synth-body"})
            j += 1
        print("%s: +%d (+%d negatives)" % (name, k, j), file=sys.stderr)
    if added:
        with open(a.cases, "a", encoding="utf-8") as fh:
            for r in added:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("appended %d synthetic positives to %s (%d skills failed; %d selftest calls)" % (
        len(added), a.cases, failed, st_calls), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
