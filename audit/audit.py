#!/usr/bin/env python3
"""Compare skill usage with your current skillOverrides and suggest changes. Read-only.

Input: the CSV from skill-usage.py. Prints a Markdown table of suggested changes,
fully listed skills whose description is over the cap, and listed descriptions that
name a hidden skill (the model will try to call it and fail).

Default: only list skills again. More than --rare uses -> on; everything else keeps its state.
A skill's description is how the model finds it, and "never used" is not "never needed": in a
repo where a 25-skill batch of never-used skills went name-only, recall on prompts written for
those skills fell from 17% to 9% for a ~100-token saving (the listing budget refills).

With --hide, the hiding rules apply too (A/B every hide before keeping it, see ab/README.md):
  typed only, never model-invoked, currently "on"  -> user-invocable-only
  typed only with an explicit state                -> keep it (e.g. a CLAUDE.md "load the X skill" pointer needs X listed)
  more than --rare uses                            -> on
  rare and fully listed (or undecided)             -> name-only if used or newer than --new-days, else user-invocable-only
  anything else                                    -> keep the explicit decision
Age is the date git first added the skill when its folder is in a repo (a clone or checkout
resets file times), else the folder's birth time.

Usage: audit.py USAGE.csv [--settings <config>/settings.json] [--skills-dir <config>/skills]
                [--cap 600] [--rare 3] [--new-days 30] [--hide]
Built-in and plugin skills (e.g. /loop, /init) are not on disk, so they don't appear here,
but skillOverrides still works on them; check the skill_listing block (measure/start_tokens.py).
<config> is $CLAUDE_CONFIG_DIR if set, else ~/.claude.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))
import ccpaths  # noqa: E402
import skill_meta  # noqa: E402
import usage_csv  # noqa: E402

HIDDEN = ("user-invocable-only", "off")


def suggest(model, typed, cur, explicit, age_days, rare, new_days, hide=False):
    uses = model + typed
    if not hide:
        return "on" if uses > rare else cur
    if typed and not model and cur == "on" and not explicit:
        return "user-invocable-only"
    if typed and not model:
        return cur
    if uses > rare:
        return "on"
    if cur == "on":
        return "name-only" if uses or age_days < new_days else "user-invocable-only"
    return cur


def added_at(d):
    """Epoch seconds git first added SKILL.md under d, or None outside a repo / untracked."""
    try:
        r = subprocess.run(["git", "-C", d, "log", "--diff-filter=A", "--format=%ct", "--", "SKILL.md"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = r.stdout.split() if r.returncode == 0 else []
    return int(lines[-1]) if lines else None


def age_days(d, now):
    t = added_at(d)
    if t is None:
        st = os.stat(d)
        t = getattr(st, "st_birthtime", st.st_mtime)
    return (now - t) / 86400


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("usage_csv")
    ap.add_argument("--settings", help="default: <config>/settings.json")
    ap.add_argument("--skills-dir", help="default: <config>/skills")
    ap.add_argument("--cap", type=int, default=600)
    ap.add_argument("--rare", type=int, default=3)
    ap.add_argument("--new-days", type=int, default=30)
    ap.add_argument("--hide", action="store_true", help="also suggest hiding rarely used skills (A/B them first)")
    a = ap.parse_args(argv)
    sp = os.path.expanduser(a.settings or ccpaths.config_path("settings.json"))
    sd = os.path.expanduser(a.skills_dir or ccpaths.config_path("skills"))
    ov = json.load(open(sp)).get("skillOverrides", {}) if os.path.exists(sp) else {}
    try:
        use = usage_csv.load_usage(a.usage_csv)
    except usage_csv.UsageSchemaError as e:
        print(e, file=sys.stderr)
        return 2
    now, rows, over, listed = time.time(), [], [], {}
    for f in sorted(glob.glob(os.path.join(sd, "*", "SKILL.md"))):
        d = os.path.dirname(f)
        sk = skill_meta.read_skill(f)
        name = sk["meta"].get("name") or os.path.basename(d)
        age = age_days(d, now)
        r = use.get(name, {})
        model, typed = r.get("model_invocations", 0), r.get("typed_invocations", 0)
        cur = ov.get(name, "on")
        sug = suggest(model, typed, cur, name in ov, age, a.rare, a.new_days, a.hide)
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
    if a.hide:
        print("\n* = newer than %d days: starts name-only (probation). Suggestions only; A/B each hide." % a.new_days)
    else:
        print("\nOnly suggests listing skills again. --hide adds hiding suggestions; A/B them before keeping any.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
