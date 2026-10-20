#!/usr/bin/env python3
"""Build a rename arm: test whether different skill NAMES change which skill fires.

For a name-only skill the name is all the model sees, so a trigger-shaped name
("going-all-in-or-about-to-claim-done") can beat an opaque one ("boil-the-ocean").

Writes, for arm NAME:
  OUT/NAME.json       {"skillOverrides": {...}}: the base map, with each renamed
                      original set to "off" and each new name set to --state
  OUT/NAME.meta.json  {"cwd": CWD, "rename": {new: old}} for run.py
  CWD/.claude/skills/<new>/SKILL.md   copies with `name:` rewritten (project skills)
  CWD/.claude/commands/<dirs>/<new>.md  command copies (--control or --rename), directory kept

run.py runs this arm's cases in CWD and maps fired names back to the originals,
so report.py compares arms on the original skill names.

NEGATIVE CONTROL (--control): instead of --rename, rename EVERY skill dir in --skills-dir
to an opaque name (skill-01, skill-02, ...) and force state name-only. Each skill stays
callable (so run.py does not exclude its positives) but the name carries no signal, so
recall should collapse versus the baseline. If it does not, the prompts are too easy
(or the model finds skills by other means) and "no loss" results prove little.
COMMANDS: with --control, every command file under --commands-dir (default: the "commands"
dir next to --skills-dir, if it exists) is renamed too. Nested files keep their directory
and only the leaf file gets an opaque name (cmd-01, ...): .claude/commands/team/dev/context.md
(full name "team:dev:context") becomes team/dev/cmd-01.md ("team:dev:cmd-01"). The meta gets
"command_rename": {old full name: new full name}, and "rename" also gets {new leaf: old full
name} so run.py's unrename maps fired command names back. --control-strip-descriptions blanks
the description of every control skill and command (strongest loss signal).
The meta gets "control": true; run.py copies that onto every record and report.py
labels the arm `control` and prints a calibration line instead of a verdict.
  make_rename_arms.py --skills-dir ~/.claude/skills --base arms/before.json \\
      --control --out arms            # arm name defaults to "control"

RENAME + COMMANDS: a key in renames.json that is not a skill dir but is a command full name
("team:dev:context") renames that command's leaf; the value is the new leaf name. The
CWD/.claude/commands dir is only replaced when this arm writes commands.

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


def strip_description(text):
    """Blank the frontmatter `description:` (including indented continuation lines)."""
    m = re.match(r"^---\n(.*?\n)---(\n|$)", text, flags=re.S)
    if not m:
        return text
    out, skipping = [], False
    for line in m.group(1).splitlines(True):
        if skipping and (line[:1] in (" ", "\t")):
            continue
        skipping = line.startswith("description:")
        out.append('description: ""\n' if skipping else line)
    return "---\n" + "".join(out) + "---" + m.group(2) + text[m.end():]


def find_commands(commands_dir):
    """Sorted relative paths (with '/') of every .md under commands_dir."""
    found = []
    for root, dirs, names in os.walk(commands_dir):
        dirs.sort()
        for n in sorted(names):
            if n.endswith(".md"):
                found.append(os.path.relpath(os.path.join(root, n), commands_dir).replace(os.sep, "/"))
    return sorted(found)


def build_commands(commands_dir, state="name-only", strip=False, rename=None, prefix=""):
    """Rename command leaves. Return (overrides, files {new rel path: text}, {old full: new full}).

    rename None: every command gets an opaque leaf (cmd-01, ...). Otherwise rename is
    {old full name: new leaf} and only those commands are written (directory kept)."""
    rels = find_commands(commands_dir)
    width = max(2, len(str(len(rels))))
    ov, files, full = {}, {}, {}
    if rename is not None:
        known = {r[:-3].replace("/", ":") for r in rels}
        for old in rename:
            if old not in known:
                raise ValueError("no skill or command %r" % old)
    for i, rel in enumerate(rels):
        parts = rel[:-3].split("/")
        old_full = ":".join(parts)
        if rename is None:
            leaf = "cmd-%0*d" % (width, i + 1)
        elif old_full in rename:
            leaf = prefix + str(rename[old_full]).split(":")[-1]
            if not NAME_RE.match(leaf):
                raise ValueError("bad command name %r (lowercase, digits, hyphens, max 64)" % leaf)
        else:
            continue
        new_rel = "/".join(parts[:-1] + [leaf]) + ".md"
        with open(os.path.join(commands_dir, *rel.split("/"))) as fh:
            text = fh.read()
        files[new_rel] = strip_description(text) if strip else text
        new_full = ":".join(parts[:-1] + [leaf])
        full[old_full] = new_full
        ov[old_full] = "off"
        ov[new_full] = state
    return ov, files, full


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skills-dir", required=True)
    ap.add_argument("--base", required=True, help="arm .json to start from (usually your baseline arm)")
    ap.add_argument("--rename", help='JSON file {"old": "new", ...} (required unless --control)')
    ap.add_argument("--control", action="store_true",
                    help="negative control: opaque names for every skill, meta control=true; state name-only "
                         "unless --state on is given (names-only control: descriptions stay listed)")
    ap.add_argument("--commands-dir", default=None,
                    help="commands dir (default: ../commands next to --skills-dir if present; an explicit "
                         "path must exist). Needs --control or --rename")
    ap.add_argument("--control-strip-descriptions", action="store_true",
                    help="with --control: blank descriptions of control skills and commands")
    ap.add_argument("--name", help="arm name (default with --control: control)")
    ap.add_argument("--out", required=True, help="arms dir")
    ap.add_argument("--prefix", default="", help="prepended to every new name")
    ap.add_argument("--state", default=None, choices=["on", "name-only"], help="default name-only")
    ap.add_argument("--cwd", default=None, help="run dir for this arm (default: OUT/NAME.cwd)")
    a = ap.parse_args(argv)
    with open(a.base) as fh:
        base = json.load(fh).get("skillOverrides", {})
    if a.control:
        a.name = a.name or "control"
        a.prefix = ""
    a.state = a.state or "name-only"
    if a.commands_dir and not (a.control or a.rename):
        ap.error("--commands-dir requires --control or --rename")
    if not a.name or not (a.control or a.rename):
        ap.error("--name and --rename are required (unless --control)")
    if a.control_strip_descriptions and not a.control:
        ap.error("--control-strip-descriptions requires --control")
    cfiles, cfull = {}, {}
    try:
        sd = os.path.expanduser(a.skills_dir)
        cd = os.path.expanduser(a.commands_dir) if a.commands_dir else os.path.join(os.path.dirname(sd.rstrip("/")), "commands")
        if a.commands_dir and not os.path.isdir(cd):
            raise ValueError("--commands-dir %s is not a directory" % a.commands_dir)
        crename = None
        if a.control:
            rename = opaque_renames(sd)
        else:
            with open(a.rename) as fh:
                allren = json.load(fh)
            rename = {k: v for k, v in allren.items() if os.path.exists(os.path.join(sd, k, "SKILL.md"))}
            crename = {k: v for k, v in allren.items() if k not in rename}
            if crename and not os.path.isdir(cd):
                raise ValueError("no skill or command %r (no commands dir at %s)" % (sorted(crename)[0], cd))
        ov, files, back = build(sd, base, rename, a.prefix, a.state)
        if a.control and a.control_strip_descriptions:
            files = {k: strip_description(v) for k, v in files.items()}
        if os.path.isdir(cd) and (a.control or crename):
            cov, cfiles, cfull = build_commands(cd, a.state, a.control_strip_descriptions, crename, a.prefix)
            ov.update(cov)
            for old, new in cfull.items():
                if new.split(":")[-1] in back:
                    raise ValueError("command name %r collides with another renamed name" % new)
                back[new.split(":")[-1]] = old
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
    if cfiles:
        shutil.rmtree(os.path.join(cwd, ".claude", "commands"), ignore_errors=True)
    for rel, text in cfiles.items():
        path = os.path.join(cwd, ".claude", "commands", *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, a.name + ".json"), "w") as fh:
        json.dump({"skillOverrides": ov}, fh, indent=2, sort_keys=True)
    with open(os.path.join(a.out, a.name + ".meta.json"), "w") as fh:
        meta = {"cwd": cwd, "rename": back, "command_rename": cfull}
        if a.control:
            meta["control"] = True
        json.dump(meta, fh, indent=2, sort_keys=True)
    print("%s: %d skills renamed%s, run dir %s" % (a.name, len(back) - len(cfull), " (control)" if a.control else "", cwd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
