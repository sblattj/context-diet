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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skills-dir", required=True)
    ap.add_argument("--base", required=True, help="arm .json to start from (usually your baseline arm)")
    ap.add_argument("--rename", required=True, help='JSON file {"old": "new", ...}')
    ap.add_argument("--name", required=True, help="arm name")
    ap.add_argument("--out", required=True, help="arms dir")
    ap.add_argument("--prefix", default="", help="prepended to every new name")
    ap.add_argument("--state", default="name-only", choices=["on", "name-only"])
    ap.add_argument("--cwd", default=None, help="run dir for this arm (default: OUT/NAME.cwd)")
    a = ap.parse_args(argv)
    with open(a.base) as fh:
        base = json.load(fh).get("skillOverrides", {})
    with open(a.rename) as fh:
        rename = json.load(fh)
    try:
        ov, files, back = build(os.path.expanduser(a.skills_dir), base, rename, a.prefix, a.state)
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
        json.dump({"cwd": cwd, "rename": back}, fh, indent=2, sort_keys=True)
    print("%s: %d skills renamed, run dir %s" % (a.name, len(back), cwd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
