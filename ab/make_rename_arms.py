#!/usr/bin/env python3
"""Build a rename arm: test whether different skill NAMES change which skill fires.

For a name-only skill the name is all the model sees, so a trigger-shaped name
("going-all-in-or-about-to-claim-done") can beat an opaque one ("boil-the-ocean").

Writes, for arm NAME:
  OUT/NAME.json       {"skillOverrides": {...}}: the base map, with each renamed
                      original set to "off" and each new name set to --state
  OUT/NAME.meta.json  {"cwd": CWD, "rename": {new: old}} for run.py
  CWD/.claude/skills/<new>/SKILL.md   copies with `name:` rewritten (project skills)

run.py runs this arm's cases in CWD and maps fired names back to the originals,
so report.py compares arms on the original skill names.

NEGATIVE CONTROL (--control): instead of --rename, rename EVERY skill dir in --skills-dir
to an opaque name (skill-01, skill-02, ...) and force state name-only. Each skill stays
callable (so run.py does not exclude its positives) but the name carries no signal, so
recall should collapse versus the baseline. If it does not, the prompts are too easy
(or the model finds skills by other means) and "no loss" results prove little.
The meta gets "control": true; run.py copies that onto every record and report.py
labels the arm `control` and prints a calibration line instead of a verdict.
  make_rename_arms.py --skills-dir ~/.claude/skills --base arms/before.json \\
      --control --out arms            # arm name defaults to "control"

Example:
  make_rename_arms.py --skills-dir ~/.claude/skills --base arms/before.json \\
      --rename renames.json --name renamed --out arms [--prefix use-this-skill-when-]
renames.json: {"old-name": "new-name", ...}. Names: lowercase, digits, hyphens, max 64.
"""
import argparse
import json
import os
import re
import shutil
import sys

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def build(skills_dir, base_overrides, rename, prefix="", state="name-only"):
    """Return (overrides, files {new: SKILL.md text}, meta_rename {new: old}). Pure apart from reads."""
    ov = dict(base_overrides)
    files, back = {}, {}
    for old, new in rename.items():
        new = prefix + new
        if not NAME_RE.match(new):
            raise ValueError("bad skill name %r (lowercase, digits, hyphens, max 64)" % new)
        src = os.path.join(skills_dir, old, "SKILL.md")
        with open(src) as fh:
            text = fh.read()
        text, n = re.subn(r"^name:.*$", "name: " + new, text, count=1, flags=re.M)
        if not n:
            text = re.sub(r"^---\n", "---\nname: %s\n" % new, text, count=1)
        files[new] = text
        ov[old] = "off"
        ov[new] = state
        back[new] = old
    return ov, files, back


def opaque_renames(skills_dir):
    """{dir name: skill-NN} for every immediate subdir with a SKILL.md (sorted, stable numbering)."""
    dirs = sorted(d for d in os.listdir(skills_dir) if os.path.exists(os.path.join(skills_dir, d, "SKILL.md")))
    width = max(2, len(str(len(dirs))))
    return {d: "skill-%0*d" % (width, i + 1) for i, d in enumerate(dirs)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skills-dir", required=True)
    ap.add_argument("--base", required=True, help="arm .json to start from (usually your baseline arm)")
    ap.add_argument("--rename", help='JSON file {"old": "new", ...} (required unless --control)')
    ap.add_argument("--control", action="store_true",
                    help="negative control: opaque names for every skill, state name-only, meta control=true")
    ap.add_argument("--name", help="arm name (default with --control: control)")
    ap.add_argument("--out", required=True, help="arms dir")
    ap.add_argument("--prefix", default="", help="prepended to every new name")
    ap.add_argument("--state", default="name-only", choices=["on", "name-only"])
    ap.add_argument("--cwd", default=None, help="run dir for this arm (default: OUT/NAME.cwd)")
    a = ap.parse_args(argv)
    with open(a.base) as fh:
        base = json.load(fh).get("skillOverrides", {})
    if a.control:
        a.name = a.name or "control"
        a.state = "name-only"
        a.prefix = ""
    if not a.name or not (a.control or a.rename):
        ap.error("--name and --rename are required (unless --control)")
    try:
        sd = os.path.expanduser(a.skills_dir)
        if a.control:
            rename = opaque_renames(sd)
        else:
            with open(a.rename) as fh:
                rename = json.load(fh)
        ov, files, back = build(sd, base, rename, a.prefix, a.state)
    except (OSError, ValueError) as e:
        print("make_rename_arms: %s" % e, file=sys.stderr)
        return 2
    cwd = os.path.abspath(a.cwd or os.path.join(a.out, a.name + ".cwd"))
    shutil.rmtree(os.path.join(cwd, ".claude", "skills"), ignore_errors=True)
    for new, text in files.items():
        d = os.path.join(cwd, ".claude", "skills", new)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w") as fh:
            fh.write(text)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, a.name + ".json"), "w") as fh:
        json.dump({"skillOverrides": ov}, fh, indent=2, sort_keys=True)
    with open(os.path.join(a.out, a.name + ".meta.json"), "w") as fh:
        json.dump(dict({"cwd": cwd, "rename": back}, **({"control": True} if a.control else {})), fh, indent=2, sort_keys=True)
    print("%s: %d skills renamed%s, run dir %s" % (a.name, len(back), " (control)" if a.control else "", cwd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
