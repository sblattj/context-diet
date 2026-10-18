#!/usr/bin/env python3
"""Cross-client skill-recall A/B: does a one-line "name index" in the instructions
file replace a native skills catalog? First client: codex (others plug in as adapters).

Three arms per client (built from Claude's skillOverrides):
  listed  every skill in the client's native catalog (today's state)
  index   skills that are `name-only` in skillOverrides are removed from the native
          catalog; the instructions file gets ONE appended paragraph naming them and
          pointing at <store>/<name>/SKILL.md
  hidden  same removal, no paragraph (control: index must beat hidden)

"Fired" for a run = the agent read some path ending /<name>/SKILL.md (or invoked the
skill natively, where the client has that). Start tokens = input tokens of the first
model request.

  run_client.py build --client codex --arms-root DIR      # build + free check (no model call)
  run_client.py check --client codex --arms-root DIR      # re-check only
  run_client.py probe --client codex --arms-root DIR --arm listed --prompt "..."
  run_client.py run   --client codex --arms-root DIR --cases cases.jsonl --out results.jsonl

Records match run.py's (arm, case, rep, label, expected, skills, start_tokens, excluded,
error, ...), so report.py reads them unchanged. A positive is excluded (no call) when
none of its expected skills has a SKILL.md in the store (e.g. a dangling link).

Adapter contract (add agy/omp/... to ADAPTERS):
  adapter.discover()                      -> {name: skill dir} of the live native catalog
  adapter.build_arm(arm, root, plan)      -> writes one arm's isolated config root
  adapter.check_arm(root, cwd)            -> {"catalog": [names], "index_present": bool, "chars": int}
  adapter.run(prompt, root, cwd, opts)    -> {"read_skills", "native_skills", "start_tokens",
                                              "error", "session_id", "read_paths"}
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import _claude

ARMS = ("listed", "index", "hidden")
INDEX_HEAD = "Other skills (not listed above)."

# Any path token ending /<name>/SKILL.md. <name> is the parent dir of SKILL.md.
SKILL_PATH_RE = re.compile(r"""([^\s'"`;|&<>()]*/([A-Za-z0-9][A-Za-z0-9._:-]*)/SKILL\.md)\b""")


# ---------------------------------------------------------------------------
# client-agnostic pieces

def index_paragraph(store, names):
    return ("%s When one matches the task, read %s/<name>/SKILL.md first: %s."
            % (INDEX_HEAD, store.rstrip("/"), ", ".join(sorted(names))))


def name_only_skills(overrides_path):
    """Base names that are `name-only` in a Claude settings file's skillOverrides."""
    with open(os.path.expanduser(overrides_path), encoding="utf-8") as fh:
        ov = json.load(fh).get("skillOverrides", {})
    return {k.split(":")[-1] for k, v in ov.items() if v == "name-only"}


def plan_arms(catalog, name_only, store):
    """catalog: {name: dir}. Returns {arm: {"visible": [...], "hidden": [...], "index": str|None}}."""
    names = sorted(catalog)
    removed = [n for n in names if n in name_only]
    kept = [n for n in names if n not in name_only]
    para = index_paragraph(store, removed)
    return {
        "listed": {"visible": names, "hidden": [], "index": None},
        "index": {"visible": kept, "hidden": removed, "index": para},
        "hidden": {"visible": kept, "hidden": removed, "index": None},
    }


def build_store(store, catalog):
    """store/<name> -> real skill dir, for every skill whose SKILL.md exists. Returns names."""
    os.makedirs(store, exist_ok=True)
    for n in os.listdir(store):
        p = os.path.join(store, n)
        if os.path.islink(p):
            os.unlink(p)
    ok = []
    for n, d in sorted(catalog.items()):
        real = os.path.realpath(d)
        if os.path.isfile(os.path.join(real, "SKILL.md")):
            os.symlink(real, os.path.join(store, n))
            ok.append(n)
    return ok


def detect_reads(texts):
    """Skill names whose SKILL.md path appears in any of `texts` (commands). Ordered, unique."""
    names, paths = [], []
    for t in texts:
        for m in SKILL_PATH_RE.finditer(t or ""):
            p, n = m.group(1), m.group(2)
            if "*" in p or n in ("<name>",):
                continue
            if n not in names:
                names.append(n)
            if p not in paths:
                paths.append(p)
    return names, paths


def model_invocable(skill_dir):
    """codex's rule: hidden from the catalog when agents/openai.yaml sets
    policy.allow_implicit_invocation: false (front-matter disable-model-invocation is ignored)."""
    if not os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
        return False
    try:
        with open(os.path.join(skill_dir, "agents", "openai.yaml"), encoding="utf-8") as fh:
            y = fh.read()
    except OSError:
        return True
    return not re.search(r"(?m)^\s+allow_implicit_invocation:\s*false\s*$", y)


def store_has(store, name):
    return os.path.isfile(os.path.join(store, name, "SKILL.md"))


def is_excluded(case, store):
    exp = case.get("skills") or []
    return case.get("label") == "positive" and bool(exp) and not any(store_has(store, s) for s in exp)


def strip_toml_tables(text, prefix):
    """Drop every [prefix...] / [[prefix...]] table (and its nested tables) from TOML text,
    plus root-level dotted keys `prefix.x = ...`. Line-based (Python 3.9 has no tomllib)."""
    out, dropping, at_root = [], False, True
    hdr = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
    for line in text.splitlines(keepends=True):
        m = hdr.match(line)
        if m:
            at_root = False
            key = m.group(1).strip().strip('"')
            dropping = key == prefix or key.startswith(prefix + ".")
            if dropping:
                continue
        elif dropping:
            continue
        elif at_root and re.match(r"^\s*%s\s*\." % re.escape(prefix), line):
            continue
        out.append(line)
    return "".join(out)


# ---------------------------------------------------------------------------
# codex adapter

class CodexAdapter:
    """codex-cli via CODEX_HOME=<arm root>. Runs go through ach (`ach run --agent codex`)."""
    name = "codex"

    def __init__(self, live_home="~/.codex", codex_bin=None, bun_bin=None, ach_ts=None,
                 user_skill_roots=("~/.codex/skills", "~/.agents/skills")):
        self.live_home = os.path.expanduser(live_home)
        self.codex_bin = codex_bin or shutil.which("codex") or "codex"
        self.bun_bin = bun_bin or shutil.which("bun") or os.path.expanduser("~/.bun/bin/bun")
        # ach.ts: --ach-ts, else $ACH_TS, else the dotai vendor checkout.
        self.ach_ts = os.path.expanduser(ach_ts or os.environ.get("ACH_TS") or
                                         "~/.dotai/vendor/agentic-coding-harness/src/cli/ach.ts")
        self.user_skill_roots = [os.path.expanduser(r) for r in user_skill_roots]

    def discover(self):
        """The live native catalog: every skill dir in the user skill roots (not .system)."""
        cat = {}
        for root in self.user_skill_roots:
            if not os.path.isdir(root):
                continue
            for n in sorted(os.listdir(root)):
                if n.startswith("."):
                    continue
                cat.setdefault(n, os.path.join(root, n))
        return cat

    def instructions_text(self):
        for fn in ("AGENTS.override.md", "AGENTS.md"):
            p = os.path.join(self.live_home, fn)
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as fh:
                    return fh.read()
        return ""

    def build_arm(self, arm, root, plan, catalog):
        """Isolated CODEX_HOME: auth symlink, config without MCP, AGENTS.md, skills/ symlinks.
        codex also reads ~/.agents/skills (outside CODEX_HOME); hidden skills are therefore
        also disabled with [[skills.config]] name=... enabled=false."""
        os.makedirs(root, exist_ok=True)
        auth = os.path.join(root, "auth.json")
        if os.path.lexists(auth):
            os.unlink(auth)
        os.symlink(os.path.join(self.live_home, "auth.json"), auth)
        skills = os.path.join(root, "skills")
        if os.path.isdir(skills):
            for n in os.listdir(skills):  # keep .system (codex installs it itself)
                p = os.path.join(skills, n)
                if os.path.islink(p):
                    os.unlink(p)
        os.makedirs(skills, exist_ok=True)
        for n in plan["visible"]:
            os.symlink(os.path.realpath(catalog[n]), os.path.join(skills, n))
        with open(os.path.join(self.live_home, "config.toml"), encoding="utf-8") as fh:
            cfg = strip_toml_tables(fh.read(), "mcp_servers")
        cfg = strip_toml_tables(cfg, "skills")
        cfg = cfg.rstrip("\n") + "\n"
        for n in plan["hidden"]:
            cfg += '\n[[skills.config]]\nname = "%s"\nenabled = false\n' % n
        with open(os.path.join(root, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write(cfg)
        text = self.instructions_text()
        if plan["index"]:
            text = text.rstrip("\n") + "\n\n" + plan["index"] + "\n"
        for fn in ("AGENTS.override.md",):
            if os.path.lexists(os.path.join(root, fn)):
                os.unlink(os.path.join(root, fn))
        with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write(text)

    @staticmethod
    def parse_prompt_input(items):
        """`codex debug prompt-input` JSON -> {"catalog": user skill names, "system": [...],
        "index_present", "agents_present", "chars"}."""
        texts = [c.get("text", "") for m in items for c in (m.get("content") or []) if isinstance(c, dict)]
        sk = next((t for t in texts if t.startswith("<skills_instructions>")), "")
        roots = dict(re.findall(r"- `(r\d+)` = `([^`]*)`", sk))
        user, system = [], []
        for r, rel in re.findall(r"\(file: (r\d+)/([^)]*?)/SKILL\.md\)", sk):
            name = rel.split("/")[-1]
            (system if roots.get(r, "").rstrip("/").endswith("/.system") else user).append(name)
        agents = next((t for t in texts if t.startswith("# AGENTS.md instructions")), "")
        return {"catalog": sorted(user), "system": sorted(system),
                "index_present": agents.count(INDEX_HEAD) == 1, "agents_present": bool(agents),
                "chars": sum(len(t) for t in texts)}

    def check_arm(self, root, cwd):
        p = subprocess.run([self.codex_bin, "debug", "prompt-input", "x"], cwd=cwd,
                           env=dict(os.environ, CODEX_HOME=root), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError("prompt-input exited %d: %s" % (p.returncode, p.stderr[-300:].decode("utf-8", "replace")))
        return self.parse_prompt_input(json.loads(p.stdout.decode("utf-8")))

    def ach_args(self, prompt, opts):
        extra = "--skip-git-repo-check --sandbox read-only"
        args = [self.ach_ts, "run", "--agent", "codex", "--wall-ms", str(int(opts.deadline * 1000)),
                "--extra-args=" + extra, "--json"]
        if getattr(opts, "model", None):
            args += ["--model", opts.model]
        return args + [prompt]

    @staticmethod
    def parse_ach(stdout):
        """ach --json stdout -> RunResult dict (last parseable JSON object)."""
        s = stdout.strip()
        try:
            return json.loads(s)
        except ValueError:
            pass
        start = s.find("{")
        while start != -1:
            try:
                return json.loads(s[start:])
            except ValueError:
                start = s.find("{", start + 1)
        raise ValueError("no JSON RunResult in ach output: %r" % s[:200])

    @staticmethod
    def tool_texts(result):
        """Command strings of tool calls in an ach RunResult: events of type tool_call carry
        `arguments` (e.g. "/bin/zsh -lc 'cat .../SKILL.md'"); older ach builds emitted
        type tool / phase start with `input`."""
        out = []
        for ev in result.get("events") or []:
            if ev.get("type") == "tool_call":
                v = ev.get("arguments")
            elif ev.get("type") == "tool" and ev.get("phase") == "start":
                v = ev.get("input")
            else:
                continue
            if v is not None:
                out.append(v if isinstance(v, str) else json.dumps(v))
        return out

    @staticmethod
    def session_id(result):
        for k in ("sessionId", "session_id", "threadId"):
            if result.get(k):
                return result[k]
        for ev in result.get("events") or []:
            for k in ("sessionId", "session_id", "thread_id"):
                if ev.get(k):
                    return ev[k]
        return None

    @staticmethod
    def find_rollout(root, session_id):
        if not session_id:
            return None
        for dp, _dn, fns in os.walk(os.path.join(root, "sessions")):
            for fn in fns:
                if fn.startswith("rollout-") and session_id in fn:
                    return os.path.join(dp, fn)
        return None

    @staticmethod
    def read_rollout(path):
        """Rollout JSONL -> (first request input tokens, [tool call texts]).
        token_count events carry per-request last_token_usage; tool calls are response_items
        (function_call arguments, custom_tool_call input such as code-mode `exec` JS,
        local_shell_call action). Tool OUTPUTS are not scanned (an `ls` listing is not a read)."""
        first, texts = None, []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                pl = ev.get("payload") or {}
                t = pl.get("type")
                if t == "token_count" and first is None and pl.get("info"):
                    u = pl["info"].get("last_token_usage") or {}
                    first = u.get("input_tokens")
                elif ev.get("type") == "response_item" and t in ("function_call", "custom_tool_call", "local_shell_call"):
                    v = pl.get("arguments") or pl.get("input") or pl.get("action")
                    texts.append(v if isinstance(v, str) else json.dumps(v))
        return first, texts

    def run(self, prompt, root, cwd, opts):
        res = {"read_skills": [], "native_skills": [], "start_tokens": None, "error": None,
               "session_id": None, "read_paths": []}
        try:
            env = {"CODEX_HOME": root,
                   "PATH": os.path.dirname(self.codex_bin) + os.pathsep + os.environ.get("PATH", "")}
            out, rc = _claude.run_claude(self.ach_args(prompt, opts), deadline=opts.deadline + 30,
                                         cwd=cwd, strip_api_key=False, extra_env=env,
                                         claude_bin=self.bun_bin)
            with open(os.path.join(cwd, ".ach-result.json"), "w", encoding="utf-8") as fh:
                fh.write(out)  # raw evidence; the run is over, the agent never sees it
            r = self.parse_ach(out)
            res["session_id"] = self.session_id(r)
            st, texts = None, self.tool_texts(r)
            rp = self.find_rollout(root, res["session_id"])
            if rp:
                st, rtexts = self.read_rollout(rp)
                texts += rtexts
            res["read_skills"], res["read_paths"] = detect_reads(texts)
            # ach's tokens[0].inputTokens is the whole codex turn (all requests, minus cache
            # reads), not the first request, so it is never used as start tokens.
            res["start_tokens"] = st
            res["start_tokens_source"] = "rollout" if st is not None else None
            res["exit_status"] = r.get("exitStatus")
            if r.get("exitStatus") != "success" and not res["read_skills"]:
                res["error"] = "ach exitStatus=%s rc=%s" % (r.get("exitStatus"), rc)
            elif st is None:
                res["error"] = "no rollout token_count for session %s" % res["session_id"]
        except Exception as e:  # noqa: BLE001 - recorded
            res["error"] = str(e)[:300]
        return res


ADAPTERS = {"codex": CodexAdapter}


# ---------------------------------------------------------------------------
# commands

def arm_root(arms_root, arm):
    return os.path.join(arms_root, arm)


def load_manifest(arms_root):
    with open(os.path.join(arms_root, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def cmd_build(a, ad):
    arms_root = os.path.abspath(a.arms_root)
    os.makedirs(arms_root, exist_ok=True)
    catalog = ad.discover()
    store = os.path.join(arms_root, "store")
    in_store = build_store(store, catalog)
    visible = {n: d for n, d in catalog.items() if n in in_store and model_invocable(os.path.join(store, n))}
    plan = plan_arms(visible, name_only_skills(a.overrides), store)
    for arm in ARMS:
        ad.build_arm(arm, arm_root(arms_root, arm), plan[arm], catalog)
    man = {"client": ad.name, "store": store, "arms": list(ARMS), "plan": plan,
           "skipped_no_skill_md": sorted(set(catalog) - set(in_store)),
           "not_model_invocable": sorted(set(in_store) - set(visible))}
    with open(os.path.join(arms_root, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(man, fh, indent=1)
    print("built %d arms under %s (store %d skills; no SKILL.md: %s; not model-invocable: %s)" % (
        len(ARMS), arms_root, len(in_store), man["skipped_no_skill_md"], man["not_model_invocable"]),
        file=sys.stderr)
    return cmd_check(a, ad)


def cmd_check(a, ad):
    arms_root = os.path.abspath(a.arms_root)
    man = load_manifest(arms_root)
    cwd = tempfile.mkdtemp(prefix="check-", dir=arms_root)
    bad = 0
    for arm in man["arms"]:
        c = ad.check_arm(arm_root(arms_root, arm), cwd)
        want = sorted(man["plan"][arm]["visible"])
        want_index = bool(man["plan"][arm]["index"])
        verdicts = [("catalog == plan", c["catalog"] == want),
                    ("agents file loaded", c["agents_present"]),
                    ("index paragraph %s" % ("once" if want_index else "absent"), c["index_present"] == want_index)]
        print("%-7s catalog=%d (+%d system) expected=%d chars=%d index=%s" % (
            arm, len(c["catalog"]), len(c["system"]), len(want), c["chars"], c["index_present"]))
        for label, ok in verdicts:
            print("  %s: %s" % ("PASS" if ok else "FAIL", label))
            bad += 0 if ok else 1
        if c["catalog"] != want:
            print("  diff: missing=%s extra=%s" % (sorted(set(want) - set(c["catalog"])),
                                                   sorted(set(c["catalog"]) - set(want))))
    shutil.rmtree(cwd, ignore_errors=True)
    return 1 if bad else 0


def run_case(ad, case, arm, arm_idx, rep, a, arms_root, store):
    rec = {"key": "%s|%s|%d" % (arm, case["id"], rep), "arm": arm, "arm_idx": arm_idx, "case": case["id"],
           "rep": rep, "label": case["label"], "expected": case.get("skills", []),
           "explicit": bool(case.get("explicit")), "synthetic": bool(case.get("synthetic")),
           "prompt": case["prompt"][:200], "ts": int(time.time()), "client": ad.name}
    if is_excluded(case, store):
        rec.update(excluded=True, skills=[], start_tokens=None)
        return rec
    t0 = time.time()
    cwd = tempfile.mkdtemp(prefix="run-", dir=os.path.join(arms_root, "cwd"))
    r = ad.run(case["prompt"], arm_root(arms_root, arm), cwd, a)
    skills = list(r["read_skills"]) + [s for s in r["native_skills"] if s not in r["read_skills"]]
    rec.update(skills=skills, start_tokens=r["start_tokens"], session_id=r.get("session_id"),
               read_paths=r.get("read_paths", []), start_tokens_source=r.get("start_tokens_source"),
               exit_status=r.get("exit_status"))
    if r["error"]:
        rec["error"] = r["error"]
    rec["secs"] = round(time.time() - t0, 1)
    return rec


def cmd_probe(a, ad):
    arms_root = os.path.abspath(a.arms_root)
    os.makedirs(os.path.join(arms_root, "cwd"), exist_ok=True)
    case = {"id": "probe", "label": "positive", "skills": [], "prompt": a.prompt}
    rec = run_case(ad, case, a.arm, 0, 0, a, arms_root, load_manifest(arms_root)["store"])
    print(json.dumps(rec, indent=1))
    return 1 if rec.get("error") else 0


def cmd_run(a, ad):
    arms_root = os.path.abspath(a.arms_root)
    man = load_manifest(arms_root)
    os.makedirs(os.path.join(arms_root, "cwd"), exist_ok=True)
    arms = a.arm or man["arms"]
    cases = _claude.read_jsonl(a.cases)
    if a.skip_skill:
        cases = [c for c in cases if not set(c.get("skills") or []) & set(a.skip_skill)]
    if a.limit:
        cases = cases[:a.limit]
    done = set()
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if a.keep_errors or not r.get("error"):
                    done.add(r["key"])
    todo = [(c, arm, man["arms"].index(arm), rep) for rep in range(a.reps) for c in cases for arm in arms
            if "%s|%s|%d" % (arm, c["id"], rep) not in done]
    prog = a.progress or a.out + ".progress"
    print("%d runs to do (%d already done)" % (len(todo), len(done)), file=sys.stderr)
    lock, n = threading.Lock(), [0, 0]

    def work(item):
        c, arm, idx, rep = item
        rec = run_case(ad, c, arm, idx, rep, a, arms_root, man["store"])
        with lock:
            with open(a.out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n[0] += 1
            n[1] += 1 if rec.get("error") else 0
            line = "[%d/%d] %s %s fired=%s tok=%s%s" % (n[0], len(todo), rec["key"], rec["expected"], rec["skills"],
                                                       rec.get("start_tokens"), " ERROR" if rec.get("error") else "")
            with open(prog, "a", encoding="utf-8") as pf:
                pf.write(line + "\n")
            print(line, file=sys.stderr, flush=True)

    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        list(ex.map(work, todo))
    print("done: %d runs, %d errors -> %s" % (n[0], n[1], a.out), file=sys.stderr)
    return 1 if n[1] else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["build", "check", "probe", "run"])
    ap.add_argument("--client", default="codex", choices=sorted(ADAPTERS))
    ap.add_argument("--arms-root", required=True, help="dir holding <arm>/ config roots, store/, manifest.json")
    ap.add_argument("--overrides", default="~/.claude/settings.json", help="Claude settings with skillOverrides")
    ap.add_argument("--cases")
    ap.add_argument("--out")
    ap.add_argument("--arm", nargs="+", help="run: subset of arms; probe: the arm (one)")
    ap.add_argument("--prompt", help="probe: the prompt")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1, help="arms share sqlite state per root; keep low")
    ap.add_argument("--deadline", type=float, default=240, help="per-run wall-clock seconds (ach --wall-ms)")
    ap.add_argument("--model", default=None, help="override the client's configured model")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-skill", nargs="*", default=[], help="drop cases expecting any of these skills")
    ap.add_argument("--progress")
    ap.add_argument("--keep-errors", action="store_true")
    ap.add_argument("--live-home", default=None, help="codex: live config root (default ~/.codex)")
    ap.add_argument("--codex-bin", default=None, help="codex: binary (default: codex on PATH)")
    ap.add_argument("--bun-bin", default=None, help="bun binary that runs ach.ts")
    ap.add_argument("--ach-ts", default=None, help="path to ach.ts (default $ACH_TS, else ~/.dotai/vendor/agentic-coding-harness/src/cli/ach.ts)")
    ap.add_argument("--skill-roots", nargs="+", default=None,
                    help="codex: user skill roots codex scans (default ~/.codex/skills ~/.agents/skills)")
    a = ap.parse_args(argv)
    kw = {k: v for k, v in (("live_home", a.live_home), ("codex_bin", a.codex_bin), ("bun_bin", a.bun_bin),
                             ("ach_ts", a.ach_ts), ("user_skill_roots", a.skill_roots)) if v}
    ad = ADAPTERS[a.client](**kw)
    if a.cmd == "probe":
        if not (a.arm and a.prompt):
            ap.error("probe needs --arm ARM --prompt TEXT")
        a.arm = a.arm[0]
        return cmd_probe(a, ad)
    if a.cmd == "run" and not (a.cases and a.out):
        ap.error("run needs --cases and --out")
    return {"build": cmd_build, "check": cmd_check, "run": cmd_run}[a.cmd](a, ad)


if __name__ == "__main__":
    sys.exit(main())
