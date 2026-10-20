#!/usr/bin/env python3
"""Kiro always-on context: static footprint plus an optional live probe.

Static (no kiro-cli needed): tokens (~chars/4) of
  - workspace .kiro/steering/*.md
  - file:// entries in the `resources` of workspace .kiro/agents/*.json
  - personal ~/.kiro/steering/*.md, reported SEPARATELY (it silently joins every session)
A steering file that is both under .kiro/steering and a file:// resource of the same agent is
loaded twice and counted twice; it is flagged as a duplicate.
Live (needs kiro-cli on PATH, else skipped with exit 0): runs one headless chat with an isolated
HOME and a hard timeout, reads the session's contextFiles token count and optionally checks an
answer (--expect REGEX) and whether the agent read files to get it (--read-pattern REGEX).
Usage: kiro_footprint.py [WORKSPACE] [--home DIR] [--json]
       kiro_footprint.py [WORKSPACE] --live --prompt TEXT [--agent NAME] [--expect RX]
                         [--read-pattern RX] [--timeout SEC] [--home DIR] [--kiro-bin PATH]
Without --home the real $HOME is read for personal steering and flagged as unisolated."""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "lib"))
from tokens import est_tokens  # noqa: E402


def _entry(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {"path": path, "real": os.path.realpath(path), "tokens": 0, "exists": False}
    return {"path": path, "real": os.path.realpath(path), "tokens": est_tokens(text), "exists": True}


def steering_files(root):
    return sorted(glob.glob(os.path.join(root, "*.md")))


def _resource_uris(res):
    if isinstance(res, str):
        return [res]
    if isinstance(res, dict):
        u = res.get("uri") or res.get("path")
        return [u] if isinstance(u, str) else []
    return []


def agent_resources(workspace, agent_json):
    """file:// paths an agent JSON loads (globs expanded; relative paths against the workspace)."""
    try:
        with open(agent_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    res = data.get("resources") if isinstance(data, dict) else None
    out = []
    for uri in (res if isinstance(res, list) else []):
        for u in _resource_uris(uri):
            if not u.startswith("file://"):
                continue
            p = u[len("file://"):]
            if not os.path.isabs(p):
                p = os.path.join(workspace, p)
            hits = sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
            out.extend(hits)
    return out


def static_footprint(workspace, home=None, isolated=None):
    """home: HOME whose ~/.kiro/steering counts as personal. isolated: True when home is a
    throwaway/controlled dir (default: True iff home was passed explicitly)."""
    explicit = home is not None
    home = home if explicit else os.path.expanduser("~")
    if isolated is None:
        isolated = explicit
    ws_steer = [_entry(p) for p in steering_files(os.path.join(workspace, ".kiro", "steering"))]
    steer_real = {e["real"] for e in ws_steer}
    agents = []
    for aj in sorted(glob.glob(os.path.join(workspace, ".kiro", "agents", "*.json"))):
        res = [_entry(p) for p in agent_resources(workspace, aj)]
        dups = [e for e in res if e["real"] in steer_real]
        for e in res:
            e["duplicate"] = e["real"] in steer_real
        agents.append({
            "agent": os.path.splitext(os.path.basename(aj))[0],
            "resources": res,
            "resource_tokens": sum(e["tokens"] for e in res),
            "duplicates": [e["path"] for e in dups],
            "duplicate_tokens": sum(e["tokens"] for e in dups),
        })
    personal = [_entry(p) for p in steering_files(os.path.join(home, ".kiro", "steering"))]
    ws_tok = sum(e["tokens"] for e in ws_steer)
    for a in agents:
        a["total_tokens"] = ws_tok + a["resource_tokens"]
    return {
        "workspace": workspace,
        "workspace_steering": ws_steer,
        "workspace_steering_tokens": ws_tok,
        "agents": agents,
        "personal_steering": personal,
        "personal_steering_tokens": sum(e["tokens"] for e in personal),
        "home": home,
        "home_isolated": bool(isolated),
    }


def render(rep):
    L = ["Kiro static footprint (tokens ~chars/4)", "workspace: " + rep["workspace"], ""]
    L.append("workspace .kiro/steering: %d tokens in %d files" % (rep["workspace_steering_tokens"], len(rep["workspace_steering"])))
    for e in rep["workspace_steering"]:
        L.append("  %6d  %s" % (e["tokens"], e["path"]))
    for a in rep["agents"]:
        L.append("agent %s: %d resource tokens, %d total with steering" % (a["agent"], a["resource_tokens"], a["total_tokens"]))
        for e in a["resources"]:
            L.append("  %6d  %s%s" % (e["tokens"], e["path"], "  DUPLICATE (loaded twice)" if e["duplicate"] else ""))
        if a["duplicates"]:
            L.append("  WARNING: %d tokens counted twice; drop the file:// entry or the steering copy" % a["duplicate_tokens"])
    L.append("")
    L.append("personal ~/.kiro/steering (separate, not in the workspace total): %d tokens in %d files"
             % (rep["personal_steering_tokens"], len(rep["personal_steering"])))
    for e in rep["personal_steering"]:
        L.append("  %6d  %s" % (e["tokens"], e["path"]))
    if rep["personal_steering"] and not rep["home_isolated"]:
        L.append("  WARNING: real HOME read; these files join EVERY session. Measure with --home <empty dir>.")
    return "\n".join(L)


def parse_stream(text):
    """Parse kiro stream-json lines into context tokens, final text, tool-call inputs."""
    final, ctx, items, calls = "", None, [], {}
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        d = e.get("data") or {}
        if e.get("type") == "runFinished":
            final = d.get("finalText", "") or final
        u = d.get("update") or {}
        cf = ((u.get("_meta") or {}).get("kiro") or {}).get("breakdown", {}).get("contextFiles")
        if cf:
            ctx, items = cf.get("tokens"), cf.get("items", [])
        if u.get("sessionUpdate") in ("tool_call", "tool_call_update") and u.get("rawInput"):
            calls.setdefault(u.get("toolCallId"), json.dumps(u["rawInput"]))
    return {"final": final, "context_tokens": ctx, "items": items, "calls": list(calls.values())}


# kiro-cli keeps its login under the platform data dir and resolves its helper binaries
# (kiro-cli-chat) from ~/.local/bin, both relative to HOME. An empty HOME therefore has no
# login and kiro waits until the timeout. Link only these into the isolated HOME, never ~/.kiro.
AUTH_PATHS = (os.path.join("Library", "Application Support", "kiro-cli"),
              os.path.join(".local", "share", "kiro-cli"))


def seed_home(home, real_home):
    """Symlink kiro-cli's login data and helper binaries from real_home into home.
    Existing entries in home are left alone. Returns the relative paths linked."""
    linked = []
    for rel in AUTH_PATHS:
        src, dst = os.path.join(real_home, rel), os.path.join(home, rel)
        if os.path.isdir(src) and not os.path.lexists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(src, dst)
            linked.append(rel)
    bindir = os.path.join(real_home, ".local", "bin")
    if os.path.isdir(bindir):
        for name in sorted(os.listdir(bindir)):
            if not name.startswith("kiro-cli"):
                continue
            rel = os.path.join(".local", "bin", name)
            dst = os.path.join(home, rel)
            if not os.path.lexists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.symlink(os.path.join(bindir, name), dst)
                linked.append(rel)
    return linked


def live_probe(workspace, prompt, agent=None, home=None, timeout=240, kiro_bin="kiro-cli",
               expect=None, read_pattern=None, link_auth=True, real_home=None):
    """Returns a dict; {"skipped": reason} when kiro-cli is missing."""
    exe = shutil.which(kiro_bin)
    if not exe:
        return {"skipped": "%s not found on PATH" % kiro_bin}
    tmp = None
    if home is None:
        tmp = tempfile.mkdtemp(prefix="kiro-home-")
        home = tmp
    linked = seed_home(home, real_home or os.path.expanduser("~")) if link_auth else []
    cmd = [exe, "chat", "--v3"] + (["--agent", agent] if agent else []) + [
        "--output-format", "stream-json", "--no-interactive", "--trust-all-tools", prompt]
    env = dict(os.environ, HOME=home)
    try:
        try:
            r = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            hint = "" if linked else " (no kiro login linked into HOME; drop --bare-home or log in there)"
            return {"error": "timeout after %ss%s" % (timeout, hint), "home": home}
        except OSError as e:
            return {"error": str(e), "home": home}
        p = parse_stream(r.stdout)
        p["returncode"] = r.returncode
        p["home"] = home
        p["linked"] = linked
        if expect is not None:
            p["answer_ok"] = bool(re.search(expect, p["final"], re.I | re.S))
        if read_pattern is not None:
            p["file_reads"] = sum(1 for c in p["calls"] if re.search(read_pattern, c))
        del p["calls"]
        return p
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("workspace", nargs="?", default=".")
    ap.add_argument("--home", help="HOME to read personal steering from / run kiro under (use an empty dir)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--prompt")
    ap.add_argument("--agent")
    ap.add_argument("--expect")
    ap.add_argument("--read-pattern")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--kiro-bin", default="kiro-cli")
    ap.add_argument("--bare-home", action="store_true",
                    help="do not link kiro-cli login data and binaries into the isolated HOME")
    a = ap.parse_args(argv)
    ws = os.path.abspath(a.workspace)
    if not a.live:
        rep = static_footprint(ws, a.home)
        print(json.dumps(rep, indent=1) if a.json else render(rep))
        return 0
    if not a.prompt:
        print("--live needs --prompt", file=sys.stderr)
        return 2
    res = live_probe(ws, a.prompt, a.agent, a.home, a.timeout, a.kiro_bin, a.expect, a.read_pattern,
                     link_auth=not a.bare_home)
    if "skipped" in res:
        print("SKIP: " + res["skipped"])
        return 0
    print(json.dumps(res, indent=1))
    return 1 if "error" in res else 0


if __name__ == "__main__":
    sys.exit(main())
