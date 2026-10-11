#!/usr/bin/env python3
"""Count MCP tool calls per server in local Claude Code transcripts; suggest denials.

Counts assistant `tool_use` blocks whose name is `mcp__<server>__<tool>`, grouped by
<server>, over the recency window (--window-days, default 30). A message split over
several JSONL lines repeats its tool_use blocks; each tool_use id is counted once.
The result is joined with the servers you have configured (config file `mcpServers`,
the scoped project's `mcpServers`, and `.mcp.json` in the project), and printed as:

  server, calls in window, last used, configured, suggestion

Suggestion: `deny` for a configured server with no calls in the window, else `keep`.
A ready-to-paste `"deniedMcpServers"` block lists the deny set. Read-only: nothing is
edited. Claude Code puts server names into tool names with non-alphanumeric characters
turned into `_` (a connector "claude.ai Some Connector" appears as
`claude_ai_Some_Connector`), so configured names are matched through that
normalization. Servers that were called but are not in any config file (claude.ai
connectors, plugin servers) are listed as configured=no and always `keep`: block those
by name with deniedMcpServers or disableClaudeAiConnectors.

Scope is the same as skill-usage.py: by default only the CURRENT project's sessions
(<config>/projects/<slug of cwd>, config = $CLAUDE_CONFIG_DIR or ~/.claude); --project
PATH picks another, --all-projects reads every project. MCP use is global, so for
pruning decisions you usually want --all-projects. Headless runs (`claude -p`, SDK) are
skipped unless --include-headless.

Config file: $CLAUDE_CONFIG_DIR/.claude.json if CLAUDE_CONFIG_DIR is set, else
~/.claude.json (override with --config).
"""
import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
import ccpaths  # noqa: E402


def _load_skill_usage():
    spec = importlib.util.spec_from_file_location("skill_usage_for_mcp",
                                                  os.path.join(HERE, "skill-usage.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


su = _load_skill_usage()
HEADER = ["server", "calls", "last_used", "configured", "suggestion"]


def normalize(name):
    """Server name as it appears inside a tool name."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _candidates(name):
    # Hyphens survive in real tool names; the plain rule replaces them. Accept both.
    return {normalize(name), re.sub(r"[^A-Za-z0-9_-]", "_", name)}


def scan_transcript(path, stats, seen, include_headless=False):
    """Accumulate stats[server] = {calls: [timestamps...]} from one transcript.

    seen is a shared set of tool_use ids so split/repeated lines count once."""
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for line in f:
            if '"mcp__' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or ev.get("type") != "assistant":
                continue
            ep = ev.get("entrypoint")
            if ep and ep != "cli" and not include_headless:
                continue
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for i, b in enumerate(content):
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name = b.get("name")
                if not (isinstance(name, str) and name.startswith("mcp__")):
                    continue
                server = name[5:].split("__", 1)[0]
                if not server:
                    continue
                key = b.get("id") or "%s#%d" % (msg.get("id") or path, i)
                if key in seen:
                    continue
                seen.add(key)
                stats.setdefault(server, []).append(ev.get("timestamp") or "")


def default_config_path():
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return ccpaths.config_path(".claude.json")
    return os.path.expanduser("~/.claude.json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _names(d):
    s = d.get("mcpServers") if isinstance(d, dict) else None
    return list(s) if isinstance(s, dict) else []


def configured_servers(config_path, project_dir, all_projects):
    """Names of configured servers: top-level, project entries, project .mcp.json."""
    cfg = _read_json(config_path)
    names = _names(cfg)
    projects = cfg.get("projects") if isinstance(cfg.get("projects"), dict) else {}
    want = os.path.realpath(os.path.expanduser(project_dir))
    for p, v in projects.items():
        if all_projects or os.path.realpath(os.path.expanduser(p)) == want:
            names += _names(v)
    names += _names(_read_json(os.path.join(want, ".mcp.json")))
    return sorted(set(names))


def build_rows(stats, configured, now, window_days=30):
    """Return (rows, unmatched_used). row = [server, calls, last_used, configured, suggestion]."""
    cutoff = now - dt.timedelta(days=window_days)

    def in_window(ts):
        t = su.parse_ts(ts)
        if t is not None and t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t is not None and cutoff <= t

    rows, matched = [], set()
    for name in configured:
        hits = [s for s in stats if s in _candidates(name)]
        matched.update(hits)
        ts = [t for s in hits for t in stats[s]]
        calls = sum(1 for t in ts if in_window(t))
        rows.append([name, calls, max(ts)[:10] if ts else "", "yes",
                     "keep" if calls else "deny"])
    unmatched = sorted(set(stats) - matched)
    for s in unmatched:
        ts = stats[s]
        calls = sum(1 for t in ts if in_window(t))
        rows.append([s, calls, max(ts)[:10], "no", "keep"])
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows, unmatched


def deny_block(rows):
    names = sorted(r[0] for r in rows if r[4] == "deny")
    if not names:
        return ""
    items = ", ".join(json.dumps({"serverName": n}) for n in names)
    return '"deniedMcpServers": [%s]' % items


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("projects_dir", nargs="?", default=None,
                    help="projects root (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects), "
                         "or one project's session dir")
    ap.add_argument("--config", help="Claude config JSON holding mcpServers "
                                     "(default: $CLAUDE_CONFIG_DIR/.claude.json or ~/.claude.json)")
    ap.add_argument("--window-days", type=int, default=30, help="recency window (default 30)")
    ap.add_argument("--now", help="ISO timestamp to treat as now (for reproducible runs)")
    ap.add_argument("--include-headless", action="store_true",
                    help="also count `claude -p` / SDK runs (default: interactive sessions only)")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print JSON instead of a table")
    fmt.add_argument("--csv", action="store_true", help="print CSV instead of a table")
    ccpaths.add_scope_args(ap)
    a = ap.parse_args(argv)

    root = a.projects_dir or su.default_projects_dir()
    if not os.path.isdir(root):
        print("no such transcript directory: %s" % root, file=sys.stderr)
        return 2
    try:
        root = ccpaths.resolve_root(root, a.project, a.all_projects)
    except ccpaths.ScopeError as e:
        print(e, file=sys.stderr)
        return 2
    now = su.parse_ts(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    if now is None:
        print("bad --now", file=sys.stderr)
        return 2
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    stats, seen = {}, set()
    for p in su.find_transcripts(root):
        scan_transcript(p, stats, seen, a.include_headless)
    configured = configured_servers(a.config or default_config_path(),
                                    a.project or os.getcwd(), a.all_projects)
    rows, unmatched = build_rows(stats, configured, now, a.window_days)
    deny = deny_block(rows)

    if a.json:
        print(json.dumps({
            "window_days": a.window_days,
            "servers": [dict(zip(HEADER, r)) for r in rows],
            "unmatched_used": unmatched,
            "deniedMcpServers": [{"serverName": r[0]} for r in rows if r[4] == "deny"],
        }, indent=2))
    elif a.csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(HEADER)
        w.writerows(rows)
    else:
        if not rows:
            print("No MCP servers configured and no MCP calls found.")
        else:
            print("| Server | Calls (%dd) | Last used | Configured | Suggestion |\n"
                  "|---|---|---|---|---|" % a.window_days)
            for r in rows:
                print("| %s | %d | %s | %s | %s |" % tuple(r))
        if unmatched:
            print("\nCalled but not in any config file (claude.ai connector or plugin server?): %s"
                  % ", ".join(unmatched))
        if deny:
            print("\nConfigured, 0 calls in %d days. Paste into settings.json:\n%s"
                  % (a.window_days, deny))
        elif rows:
            print("\nNothing to deny.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
