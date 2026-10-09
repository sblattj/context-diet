#!/usr/bin/env python3
"""Compare skill usage with your current skillOverrides and suggest changes. Read-only.

Input: the CSV from skill-usage.py. Prints a Markdown table of suggested changes,
fully listed skills whose description is over the cap, and listed descriptions that
name a hidden skill (the model will try to call it and fail).

Rules:
  typed only, never model-invoked, currently "on"  -> user-invocable-only
  typed only with an explicit state                -> keep it (e.g. a CLAUDE.md "load the X skill" pointer needs X listed)
  more than --rare uses                            -> on
  rare and fully listed (or undecided)             -> name-only if used or newer than --new-days, else user-invocable-only
  anything else                                    -> keep the explicit decision

Usage: audit.py USAGE.csv [--settings ~/.claude/settings.json] [--skills-dir ~/.claude/skills]
                [--cap 600] [--rare 3] [--new-days 30]
Built-in and plugin skills (e.g. /loop, /init) are not on disk, so they don't appear here,
but skillOverrides still works on them; check the skill_listing block (measure/start_tokens.py).
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_meta  # noqa: E402

HIDDEN = ("user-invocable-only", "off")


def suggest(model, typed, cur, explicit, age_days, rare, new_days):
    uses = model + typed
    if typed and not model and cur == "on" and not explicit:
        return "user-invocable-only"
    if typed and not model:
        return cur
    if uses > rare:
        return "on"
    if cur == "on":
        return "name-only" if uses or age_days < new_days else "user-invocable-only"
    return cur


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("usage_csv")
    ap.add_argument("--settings", default="~/.claude/settings.json")
    ap.add_argument("--skills-dir", default="~/.claude/skills")
    ap.add_argument("--cap", type=int, default=600)
    ap.add_argument("--rare", type=int, default=3)
    ap.add_argument("--new-days", type=int, default=30)
    a = ap.parse_args(argv)
    sp, sd = os.path.expanduser(a.settings), os.path.expanduser(a.skills_dir)
    ov = json.load(open(sp)).get("skillOverrides", {}) if os.path.exists(sp) else {}
    use = {r["skill"]: r for r in csv.DictReader(open(a.usage_csv))}
    now, rows, over, listed = time.time(), [], [], {}
    for f in sorted(glob.glob(os.path.join(sd, "*", "SKILL.md"))):
        d = os.path.dirname(f)
        sk = skill_meta.read_skill(f)
        name = sk["meta"].get("name") or os.path.basename(d)
        st = os.stat(d)
        age = (now - getattr(st, "st_birthtime", st.st_mtime)) / 86400
        r = use.get(name, {})
        model, typed = int(r.get("model_invocations") or 0), int(r.get("typed_invocations") or 0)
        cur = ov.get(name, "on")
        sug = suggest(model, typed, cur, name in ov, age, a.rare, a.new_days)
        if sug != cur:
            rows.append((name + ("*" if age < a.new_days else ""), cur, sug, model, typed, r.get("last_used", "")))
        n = skill_meta.listing_length(sk["meta"])
        if cur == "on":
            listed[name] = sk["meta"].get("description", "") or ""
            if n > a.cap:
                over.append((n, name))
    print("| Skill | Now | Suggest | Model | Typed | Last used |\n|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (r[2], r[0])):
        print("| %s | %s | %s | %d | %d | %s |" % r)
    print("\nFully listed descriptions over %d chars:" % a.cap)
    for n, name in sorted(over, reverse=True):
        print("  %5d  %s" % (n, name))
    if not over:
        print("  none")
    hidden = [k for k, v in ov.items() if v in HIDDEN]
    named = [(n, h) for n, d in listed.items() for h in hidden
             if re.search(r"(?<![\w/-])" + re.escape(h) + r"(?![\w-])", d)]
    print('\nListed descriptions naming a hidden skill (say "the user command /x" instead):')
    for n, h in named:
        print("  %s -> %s" % (n, h))
    if not named:
        print("  none")
    print("\n* = newer than %d days: starts name-only (probation). Suggestions only." % a.new_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
