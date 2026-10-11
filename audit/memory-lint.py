#!/usr/bin/env python3
"""Lint the always-loaded instruction files (CLAUDE.md and what it @-imports).

Everything in these files is loaded at every session start, so dangling
pointers and rules repeated across files are a silent, recurring token cost.

Inputs: with no arguments, the global CLAUDE.md under the Claude config dir.
--project DIR adds that project's CLAUDE.md and .claude/CLAUDE.md. Explicit
paths replace the defaults. Lines starting with `@` import another file
(followed recursively; `~` and paths relative to the importing file resolve).

Rules: ML001 error   @ import target does not exist
       ML002 warning backticked or linked file path that does not exist
       ML003 warning import cycle
       ML004 warning same instruction (normalized) in more than one place
       ML005 info    file over 60 lines with no pointer out (no import, no
                     reference to a skill or another file)
Code fences are ignored. Exit status: 1 if any ML001, 2 usage error, else 0.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))
import ccpaths  # noqa: E402

MIN_DUP_CHARS = 40
LONG_FILE_LINES = 60
SEVERITY = {"ML001": "error", "ML002": "warning", "ML003": "warning",
            "ML004": "warning", "ML005": "info"}

IMPORT_RE = re.compile(r"^@(\S+)")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SKILL_RE = re.compile(r"\bskills?\b|SKILL\.md", re.I)
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def finding(rule, path, line, msg, fix, **extra):
    d = {"rule": rule, "severity": SEVERITY[rule], "file": path, "line": line,
         "message": msg, "fix": fix}
    d.update(extra)
    return d


def prose_lines(text):
    """Yield (lineno, line) outside code fences."""
    fenced = False
    for i, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield i, line


def path_candidate(tok):
    """Return the file path a token names, or None if it is not a checkable path."""
    tok = tok.strip()
    if not tok or any(c in tok for c in "<$*?[]{} \t") or "://" in tok \
            or tok.startswith(("#", "mailto:")):
        return None
    tok = tok.split("#", 1)[0]
    tok = re.sub(r":\d+(-\d+)?$", "", tok)
    if not tok:
        return None
    if tok.startswith(("/", "~", "./", "../")):
        return tok if len(tok) > 1 else None
    # bare relative path: needs a slash and an extension on the last segment
    if "/" in tok and re.search(r"\.\w+$", tok.rstrip("/").rsplit("/", 1)[-1]):
        return tok
    return None


def resolve(target, base_dir):
    return os.path.normpath(os.path.join(base_dir, os.path.expanduser(target)))


def normalize(line):
    s = line.strip()
    s = re.sub(r"^(>\s*)*([-*+]\s+|\d+[.)]\s+)", "", s)
    s = s.replace("*", "")
    s = re.sub(r"(?<!\w)_+|_+(?!\w)", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def lint(roots):
    """Return (findings, files) for the root instruction files."""
    findings, files, seen = [], [], set()
    dup = {}  # normalized -> [(path, line)]

    def visit(path, stack):
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        text = read(path)
        files.append(path)
        chain = stack + [real]
        base = os.path.dirname(os.path.abspath(path))
        n_lines = len(text.splitlines())
        pointers = 0
        for i, line in prose_lines(text):
            m = IMPORT_RE.match(line)
            if m:
                pointers += 1
                target = resolve(m.group(1), base)
                if not os.path.isfile(target):
                    findings.append(finding(
                        "ML001", path, i, "import target does not exist: %s" % m.group(1),
                        "create the file or remove the @ line"))
                elif os.path.realpath(target) in chain:
                    findings.append(finding(
                        "ML003", path, i, "import cycle: %s -> %s" % (
                            " -> ".join(os.path.basename(p) for p in chain),
                            os.path.basename(target)),
                        "remove the @ line that points back up the chain"))
                else:
                    visit(target, chain)
                continue
            if SKILL_RE.search(line):
                pointers += 1
            toks = BACKTICK_RE.findall(line) + LINK_RE.findall(line)
            for tok in toks:
                p = path_candidate(tok)
                if p is None:
                    continue
                pointers += 1
                if not os.path.exists(resolve(p, base)):
                    findings.append(finding(
                        "ML002", path, i, "referenced path does not exist: %s" % tok,
                        "fix the path or drop the reference"))
            norm = normalize(line)
            if len(norm) >= MIN_DUP_CHARS and not line.lstrip().startswith("#"):
                dup.setdefault(norm, []).append((path, i))
        if n_lines > LONG_FILE_LINES and pointers == 0:
            findings.append(finding(
                "ML005", path, 1, "%d lines and no pointer to a skill or another file" % n_lines,
                "move procedures into skills and leave a one-line pointer here"))

    for r in roots:
        visit(r, [])
    for norm, locs in dup.items():
        if len(locs) > 1:
            first = locs[0]
            findings.append(finding(
                "ML004", first[0], first[1],
                "same instruction at %d places: %s" % (
                    len(locs), ", ".join("%s:%d" % l for l in locs)),
                "keep one copy and delete or point to it from the others",
                locations=[{"file": f, "line": n} for f, n in locs]))
    findings.sort(key=lambda f: (f["file"], f["line"], f["rule"]))
    return findings, files


def default_roots(project):
    roots = [ccpaths.config_path("CLAUDE.md")]
    if project:
        pd = os.path.expanduser(project)
        roots += [os.path.join(pd, "CLAUDE.md"), os.path.join(pd, ".claude", "CLAUDE.md")]
    return [r for r in roots if os.path.isfile(r)]


def main(argv=None):
    doc = __doc__.split("\n\n")
    ap = argparse.ArgumentParser(description=doc[0], epilog="\n\n".join(doc[1:]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="instruction files to lint (default: the global CLAUDE.md)")
    ap.add_argument("--project", metavar="DIR", help="also lint DIR/CLAUDE.md and DIR/.claude/CLAUDE.md")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    if a.paths:
        missing = [p for p in a.paths if not os.path.isfile(os.path.expanduser(p))]
        if missing:
            print("not a file: %s" % ", ".join(missing), file=sys.stderr)
            return 2
        roots = [os.path.expanduser(p) for p in a.paths]
    else:
        roots = default_roots(a.project)
    if not roots:
        print("no instruction files found (looked under %s)" % ccpaths.config_dir(), file=sys.stderr)
        return 2
    findings, files = lint(roots)
    errors = sum(1 for f in findings if f["severity"] == "error")
    if a.json:
        print(json.dumps({"files": files, "findings": findings, "errors": errors}, indent=2))
    else:
        for f in findings:
            print("%s:%d: %s %s: %s\n  fix: %s" % (f["file"], f["line"], f["rule"],
                                                  f["severity"], f["message"], f["fix"]))
        print("%d file(s) checked, %d finding(s), %d error(s)" % (len(files), len(findings), errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
