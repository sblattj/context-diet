#!/usr/bin/env python3
"""Pin the size of everything Claude Code loads into each session; fail when it grows.

Tracked entries (under the Claude config dir, plus --project DIR):
  claude-md   global and project CLAUDE.md, with @imports followed recursively.
              Imported text counts toward the importer's total AND is listed as its
              own `import` entry, so moving text into an import cannot hide growth.
  agent       agents/*.md (file bytes)
  skill       listing cost per skills/*/SKILL.md: description + when_to_use length when
              skillOverrides is on/absent, name length when name-only, nothing when off
              or user-invocable-only.

--baseline FILE: if FILE is missing it is written (exit 0). Otherwise sizes are compared:
GREW / NEW entries exit 1; SHRANK / GONE are reported and exit 0. --update rewrites FILE.

Exit status: 0 ok, 1 something grew or is new, 2 usage error.
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
import ccpaths  # noqa: E402
import skill_meta  # noqa: E402


def _load_lint():
    spec = importlib.util.spec_from_file_location("skill_lint", os.path.join(HERE, "skill-lint.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def import_paths(text, base_dir):
    """Paths named by `@path` lines (outside code fences), resolved against base_dir."""
    out, fence = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence or not line.startswith("@") or len(line.strip()) < 2:
            continue
        tok = line[1:].split()[0]
        p = os.path.expanduser(tok)
        out.append(p if os.path.isabs(p) else os.path.join(base_dir, p))
    return out


def md_total(path, stack=()):
    """(total bytes incl. imports, [(import_path, import_total), ...]) ; cycle safe."""
    real = os.path.realpath(path)
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return 0, []
    if real in stack:
        return 0, []
    total, imports = len(raw), []
    stack = stack + (real,)
    for ip in import_paths(raw.decode("utf-8", "replace"), os.path.dirname(path)):
        if not os.path.isfile(ip) or os.path.realpath(ip) in stack:
            continue
        t, sub = md_total(ip, stack)
        total += t
        imports.append((ip, t))
        imports.extend(sub)
    return total, imports


def label(path, roots):
    for tag, root in roots:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
        if not rel.startswith(".."):
            return "%s:%s" % (tag, rel)
    return os.path.realpath(path)


def collect(project=None):
    cfg = ccpaths.config_dir()
    roots = [("config", cfg)] + ([("project", project)] if project else [])
    sizes = {}

    def add_md(tag, path):
        if not os.path.isfile(path):
            return
        total, imports = md_total(path)
        sizes["claude-md %s" % label(path, roots)] = total
        for ip, t in imports:
            sizes.setdefault("import %s" % label(ip, roots), t)

    add_md("config", os.path.join(cfg, "CLAUDE.md"))
    if project:
        add_md("project", os.path.join(project, "CLAUDE.md"))
        add_md("project", os.path.join(project, ".claude", "CLAUDE.md"))

    adir = os.path.join(cfg, "agents")
    if os.path.isdir(adir):
        for fn in sorted(os.listdir(adir)):
            p = os.path.join(adir, fn)
            if fn.endswith(".md") and os.path.isfile(p):
                sizes["agent %s" % fn] = os.path.getsize(p)

    settings = os.path.join(cfg, "settings.json")
    try:
        overrides = _load_lint().load_overrides(settings) if os.path.isfile(settings) else {}
    except (OSError, ValueError):
        overrides = {}
    sdir = os.path.join(cfg, "skills")
    if os.path.isdir(sdir):
        for d in sorted(os.listdir(sdir)):
            p = os.path.join(sdir, d, "SKILL.md")
            sk = skill_meta.read_skill(p) if os.path.isfile(p) else None
            if sk is None:
                continue
            name = sk["meta"].get("name") or d
            state = overrides.get(name, "on")
            if state == "on":
                n = skill_meta.listing_length(sk["meta"])
            elif state == "name-only":
                n = len(name)
            else:
                continue
            sizes["skill %s" % name] = n
    return sizes


def compare(old, new):
    """Return (lines, failed)."""
    lines, failed = [], False
    for k in sorted(new):
        if k not in old:
            lines.append("NEW %s %d bytes" % (k, new[k]))
            failed = True
        elif new[k] > old[k]:
            lines.append("GREW %s +%d bytes (%d -> %d)" % (k, new[k] - old[k], old[k], new[k]))
            failed = True
        elif new[k] < old[k]:
            lines.append("SHRANK %s -%d bytes (%d -> %d)" % (k, old[k] - new[k], old[k], new[k]))
    for k in sorted(set(old) - set(new)):
        lines.append("GONE %s (was %d bytes)" % (k, old[k]))
    return lines, failed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog="\n\n".join(__doc__.split("\n\n")[1:]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", metavar="FILE.json", required=True,
                    help="pinned sizes; written if missing")
    ap.add_argument("--project", metavar="DIR", help="also track DIR/CLAUDE.md and DIR/.claude/CLAUDE.md")
    ap.add_argument("--update", action="store_true", help="rewrite the baseline with current sizes")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    project = os.path.expanduser(a.project) if a.project else None
    if project and not os.path.isdir(project):
        print("no such project dir: %s" % project, file=sys.stderr)
        return 2
    new = collect(project)

    def write():
        with open(a.baseline, "w", encoding="utf-8") as f:
            json.dump(new, f, indent=2, sort_keys=True)
            f.write("\n")

    if not os.path.exists(a.baseline) or a.update:
        write()
        msg = "pinned %d entries in %s" % (len(new), a.baseline)
        print(json.dumps({"pinned": len(new), "baseline": a.baseline}) if a.json else msg)
        return 0
    try:
        with open(a.baseline, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError) as e:
        print("cannot read baseline %s: %s" % (a.baseline, e), file=sys.stderr)
        return 2
    lines, failed = compare(old, new)
    if a.json:
        print(json.dumps({"ok": not failed, "changes": lines, "entries": len(new)}, indent=2))
    else:
        for ln in lines:
            print(ln)
        print("%d entries checked, %s" % (len(new), "FAIL" if failed else "ok"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
