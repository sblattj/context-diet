#!/usr/bin/env python3
"""Lint hidden-skill handoffs: a command or skill hidden from the model must never be handed off to.

A command or skill hidden from the model (frontmatter `disable-model-invocation: true`, or a
skillOverrides state of user-invocable-only / off) cannot be invoked by Claude. A line such as
"then run /ops:deploy" in another command, skill, agent or CLAUDE.md is a dead end, because the
reader of that line is the model. A reference is allowed only when the text right before it tells
the USER to type or run it: "ask the user to type /x", "the user can run /x". A bare "you" never
allows, since agent prompts address the model as "you". Each reference is judged on its own.

Hidden set: by default the commands under .claude/commands and skills under .claude/skills whose
frontmatter sets `disable-model-invocation: true`. --hidden FILE replaces that with an explicit JSON
list (an array of ids, or {"commands": [...]}); ids may use `/` or `:` between parts ("ops/deploy",
"ops:deploy"). --settings FILE (repeatable) adds every skillOverrides entry in a hiding state.

Reference syntax is configurable: --prefix (default `/`), --separator (default `:`) and
--namespace NAME (repeatable, an optional leading part such as `/acme:ops:deploy` that is dropped
before matching). `/ns:name` and `/ns:group:name` are matched by default; a lone `/name` is matched
only with --bare, because it is hard to tell from a path or a built-in.

Scan roots, relative to --root (default the current directory): .claude/commands, .claude/skills,
.claude/agents, CLAUDE.md; override with --scan (repeatable). Directories contribute .md, .yaml and
.yml files; markdown frontmatter is not scanned. Exit status: 1 if any finding, 2 usage error, else 0.
"""
import argparse
import json
import os
import re
import sys

DEFAULT_SCAN = [".claude/commands", ".claude/skills", ".claude/agents", "CLAUDE.md"]
HIDING_STATES = {"user-invocable-only", "off", "disable-model-invocation"}
SEG = r"[a-z0-9][a-z0-9-]*"
SCAN_EXT = (".md", ".yaml", ".yml")

# Text right before a reference that tells the user (not the model) to type it.
ALLOW_USER_TYPES = [
    re.compile(r"\b(?:ask|tell) the user to (?:type|run)\s+`?$", re.I),
    re.compile(r"\bthe (?:user|developer)\s+(?:(?:can|could|should|must|may|will)\s+)?(?:type|run)\s+`?$", re.I),
]

FRONT_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.S)
HIDDEN_FM_RE = re.compile(r"^disable-model-invocation:\s*true\s*$", re.M)


class UsageError(Exception):
    pass


def split_front_matter(text):
    """Return (front matter text, number of lines it spans)."""
    m = FRONT_RE.match(text)
    return (m.group(0), m.group(0).count("\n")) if m else ("", 0)


def norm_id(raw, namespaces=(), separator=":"):
    """Canonical id: parts joined by ':', no leading namespace, no slashes."""
    parts = [p for p in re.split(r"[/:]|" + re.escape(separator), raw.strip().lstrip("/")) if p]
    if len(parts) > 1 and parts[0] in namespaces:
        parts = parts[1:]
    return ":".join(parts)


def ref_regex(prefix="/", separator=":"):
    p, s = re.escape(prefix), re.escape(separator)
    return re.compile(r"(?<![\w/.:~-])" + p + "(" + SEG + "(?:" + s + SEG + ")*)(?![\\w-]|/|\\.\\w)")


def walk(path, exts=SCAN_EXT):
    if os.path.isfile(path):
        return [path]
    out = []
    for d, dirs, files in os.walk(path):
        dirs.sort()
        out.extend(os.path.join(d, f) for f in sorted(files) if f.endswith(exts))
    return out


def hidden_from_front_matter(root, namespaces=(), separator=":"):
    ids = set()
    cmds = os.path.join(root, ".claude", "commands")
    for f in walk(cmds, (".md",)) if os.path.isdir(cmds) else []:
        if f.endswith(".md") and HIDDEN_FM_RE.search(split_front_matter(read(f))[0]):
            ids.add(norm_id(os.path.relpath(f, cmds)[:-3].replace(os.sep, "/"), namespaces, separator))
    skills = os.path.join(root, ".claude", "skills")
    for f in walk(skills, ("SKILL.md",)) if os.path.isdir(skills) else []:
        if os.path.basename(f) == "SKILL.md" and HIDDEN_FM_RE.search(split_front_matter(read(f))[0]):
            ids.add(norm_id(os.path.basename(os.path.dirname(f)), namespaces, separator))
    return sorted(ids)


def hidden_from_list(path, namespaces=(), separator=":"):
    try:
        data = json.loads(read(path))
    except (OSError, ValueError) as e:
        raise UsageError("--hidden %s: %s" % (path, e))
    ids = data if isinstance(data, list) else data.get("commands") if isinstance(data, dict) else None
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise UsageError('--hidden %s must be a JSON array of strings or {"commands": [...]}' % path)
    return sorted({norm_id(x, namespaces, separator) for x in ids})


def hidden_from_settings(path, namespaces=(), separator=":"):
    try:
        data = json.loads(read(path))
    except (OSError, ValueError) as e:
        raise UsageError("--settings %s: %s" % (path, e))
    ov = data.get("skillOverrides", {}) if isinstance(data, dict) else {}
    ids = set()
    for name, v in (ov.items() if isinstance(ov, dict) else []):
        state = v.get("state") if isinstance(v, dict) else v
        if state in HIDING_STATES:
            ids.add(norm_id(name, namespaces, separator))
    return sorted(ids)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def scan_files(root, scans):
    files, seen = [], set()
    for s in scans:
        p = s if os.path.isabs(s) else os.path.join(root, s)
        for f in (walk(p) if os.path.exists(p) else []):
            if f not in seen:
                seen.add(f)
                files.append(f)
    return files


def lint(root, hidden, scans=None, prefix="/", separator=":", namespaces=(), bare=False):
    """Return (findings, files scanned). Each finding: file, line, ref, command."""
    hidden = set(hidden)
    scans = DEFAULT_SCAN if scans is None else scans
    files = scan_files(root, scans)
    findings = []
    if not hidden:
        return findings, files
    rx = ref_regex(prefix, separator)
    for f in files:
        text = read(f)
        skip = split_front_matter(text)[1] if f.endswith(".md") else 0
        for i, line in enumerate(text.split("\n")):
            if i < skip:
                continue
            for m in rx.finditer(line):
                parts = m.group(1).split(separator)
                stripped = len(parts) > 1 and parts[0] in namespaces
                if stripped:
                    parts = parts[1:]
                if len(parts) == 1 and not stripped and not bare:
                    continue
                cid = ":".join(parts)
                if cid in hidden and not any(a.search(line[:m.start()]) for a in ALLOW_USER_TYPES):
                    findings.append({"file": os.path.relpath(f, root).replace(os.sep, "/"),
                                     "line": i + 1, "ref": m.group(0), "command": cid})
    return findings, files


def main(argv=None):
    doc = __doc__.split("\n\n")
    ap = argparse.ArgumentParser(description=doc[0], epilog="\n\n".join(doc[1:]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="project root (default: the current directory)")
    ap.add_argument("--scan", action="append", metavar="PATH",
                    help="file or directory to scan, relative to --root (repeatable; replaces the defaults)")
    ap.add_argument("--hidden", metavar="FILE", help="JSON list of hidden ids; replaces frontmatter detection")
    ap.add_argument("--settings", action="append", default=[], metavar="FILE",
                    help="settings.json whose skillOverrides add hidden ids (repeatable)")
    ap.add_argument("--prefix", default="/", help="reference prefix (default /)")
    ap.add_argument("--separator", default=":", help="namespace separator (default :)")
    ap.add_argument("--namespace", action="append", default=[], metavar="NAME",
                    help="optional leading namespace dropped from refs and ids (repeatable)")
    ap.add_argument("--bare", action="store_true", help="also match a lone /name with no separator")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(a.root))
    try:
        if not os.path.isdir(root):
            raise UsageError("not a directory: %s" % root)
        if not a.prefix or not a.separator:
            raise UsageError("--prefix and --separator must not be empty")
        ns, sep = tuple(a.namespace), a.separator
        hidden = set(hidden_from_list(a.hidden, ns, sep) if a.hidden
                     else hidden_from_front_matter(root, ns, sep))
        for s in a.settings:
            hidden.update(hidden_from_settings(os.path.expanduser(s), ns, sep))
    except UsageError as e:
        print("handoff-lint: %s" % e, file=sys.stderr)
        return 2
    findings, files = lint(root, hidden, a.scan, a.prefix, sep, ns, a.bare)
    if a.json:
        print(json.dumps({"hidden": sorted(hidden), "files": len(files), "findings": findings}, indent=2))
    else:
        for f in findings:
            print("%s:%d  %s  (hidden command %s)" % (f["file"], f["line"], f["ref"], f["command"]))
        print("handoff lint: %d finding(s), %d hidden command(s), %d file(s) scanned"
              % (len(findings), len(hidden), len(files)))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
