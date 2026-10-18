"""run_client.py: arm building, free check, detector, runner records -> report.py. No network."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report  # noqa: E402
import run_client as rc  # noqa: E402

# Fake `codex debug prompt-input`: renders the catalog from $CODEX_HOME/skills (+ an extra
# user root, like ~/.agents/skills) minus [[skills.config]] enabled=false names, plus AGENTS.md.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, re, sys
home = os.environ["CODEX_HOME"]
cfg = open(os.path.join(home, "config.toml")).read()
off = set(re.findall(r'\[\[skills\.config\]\]\nname = "([^"]+)"\nenabled = false', cfg))
roots = [os.path.join(home, "skills"), os.environ["FAKE_EXTRA_ROOT"]]
lines = ["<skills_instructions>", "### Skill roots"]
lines += ["- `r%d` = `%s`" % (i, r) for i, r in enumerate(roots)] + ["- `r9` = `%s/skills/.system`" % home]
seen = set()
for i, r in enumerate(roots):
    for n in sorted(os.listdir(r)):
        if n.startswith(".") or n in off or n in seen or not os.path.isfile(os.path.join(r, n, "SKILL.md")):
            continue
        seen.add(n)
        lines.append("- %s: d (file: r%d/%s/SKILL.md)" % (n, i, n))
lines.append("- sysskill: d (file: r9/sysskill/SKILL.md)")
items = [{"role": "developer", "content": [{"type": "input_text", "text": "\n".join(lines)}]}]
ag = os.path.join(home, "AGENTS.md")
if os.path.exists(ag):
    items.append({"role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions\n" + open(ag).read()}]})
print(json.dumps(items))
'''

# Fake bun running ach.ts: last argv = prompt. "please X" reads store/X/SKILL.md (via the store
# path parsed from AGENTS.md when present). Writes a codex rollout under $CODEX_HOME/sessions.
FAKE_BUN = r'''#!/usr/bin/env python3
import json, os, re, sys, uuid
a = sys.argv[1:]
log = os.environ.get("FAKE_BUN_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"args": a, "codex_home": os.environ.get("CODEX_HOME"), "cwd": os.getcwd()}) + "\n")
prompt = a[-1]
home = os.environ["CODEX_HOME"]
agents = open(os.path.join(home, "AGENTS.md")).read()
m = re.search(r"read (\S+)/<name>/SKILL\.md first", agents)
sid = str(uuid.uuid4())
calls = []
w = re.search(r"please (\S+)", prompt)
if w and m:
    calls.append({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                  "input": 'const r = await tools.exec_command({cmd:"cat %s/%s/SKILL.md"});' % (m.group(1), w.group(1))}})
calls.append({"type": "response_item", "payload": {"type": "function_call", "name": "shell",
              "arguments": json.dumps({"command": ["ls", "/x/*/SKILL.md"]})}})
d = os.path.join(home, "sessions", "2026", "10", "17")
os.makedirs(d, exist_ok=True)
toks = 1000 + 5 * len(agents)
with open(os.path.join(d, "rollout-2026-10-17T00-00-00-%s.jsonl" % sid), "w") as f:
    f.write(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
    f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": toks}, "total_token_usage": {"input_tokens": toks}}}}) + "\n")
    for c in calls:
        f.write(json.dumps(c) + "\n")
    f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": toks + 50}, "total_token_usage": {"input_tokens": 2 * toks + 50}}}}) + "\n")
print("[sandbox] header noise", file=sys.stderr)
print(json.dumps({"exitStatus": "success", "sessionId": sid, "events": [
    {"type": "tool_call", "functionName": "shell", "arguments": "/bin/zsh -lc 'pwd'"}],
    "tokens": [{"inputTokens": 99999}]}))
'''


# Fake `agy -p PROMPT --output-format stream-json`: renders the prompt from $HOME exactly where
# agy reads it (~/.gemini/config/{GEMINI.md,skills}, ~/.agents/skills; front-matter
# disable-model-invocation hides a skill), stores it in a conversation db (a small row 0, then
# the prompt row), and streams step_updates. "please X" views the listed SKILL.md when X is in
# the catalog, else <store>/X/SKILL.md when GEMINI.md has the index paragraph.
FAKE_AGY = r'''#!/usr/bin/env python3
import json, os, re, sqlite3, sys, uuid
a = sys.argv[1:]
home = os.environ["HOME"]
log = os.environ.get("FAKE_AGY_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"args": a, "home": home, "cwd": os.getcwd()}) + "\n")
prompt = a[a.index("-p") + 1]
cfg = os.path.join(home, ".gemini", "config")
gm = open(os.path.join(cfg, "GEMINI.md")).read() if os.path.exists(os.path.join(cfg, "GEMINI.md")) else ""
ents = {}
for r in (os.path.join(cfg, "skills"), os.path.join(home, ".agents", "skills")):
    for n in sorted(os.listdir(r)) if os.path.isdir(r) else []:
        md = os.path.join(r, n, "SKILL.md")
        if os.path.isfile(md) and "disable-model-invocation: true" not in open(md).read():
            ents.setdefault(n, md)
bi = os.path.join(home, ".gemini", "antigravity-cli", "builtin", "skills", "guide")
os.makedirs(bi, exist_ok=True)
ents["guide"] = os.path.join(bi, "SKILL.md")
text = "<user_rules><RULE[user_global]>%s</user_rules>\n<skills>\nintro\n%s\n</skills>" % (
    gm, "\n".join("- %s (%s): d" % (n, p) for n, p in sorted(ents.items())))
cid = str(uuid.uuid4())
d = os.path.join(home, ".gemini", "antigravity-cli", "conversations")
os.makedirs(d, exist_ok=True)
db = sqlite3.connect(os.path.join(d, cid + ".db"))
db.execute("create table gen_metadata (idx integer primary key, data blob, size integer)")
db.execute("insert into gen_metadata values (0, ?, 3)", (b"\x08\x01\x12\x01x",))
db.execute("insert into gen_metadata values (1, ?, 1)", (text.encode(),))
db.commit()
db.close()
toks = 1000 + 5 * len(text)
evs = [{"event": "init", "conversation_id": cid, "init": {"cwd": os.getcwd()}},
       {"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "step_type": "agent_response",
        "state": "DONE", "usage": {"input_tokens": toks, "cache_read_tokens": 7}}}]
w = re.search(r"please (\S+)", prompt)
m = re.search(r"read (\S+)/<name>/SKILL\.md first: ([^.]*)\.", gm)
target = None
if w and w.group(1) in ents:
    target = ents[w.group(1)]
elif w and m and w.group(1) in m.group(2).split(", "):
    target = "%s/%s/SKILL.md" % (m.group(1), w.group(1))
for st in ("ACTIVE", "DONE"):
    evs.append({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 2, "state": st,
                "step_type": "tool", "tool_name": "list_dir", "tool_info": {"name": "list_dir",
                "parameters": {"DirectoryPath": "/x/*/SKILL.md"}, "output": "a/elsewhere/SKILL.md"}}})
if target:
    for st in ("ACTIVE", "DONE"):
        evs.append({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 3, "state": st,
                    "step_type": "tool", "tool_name": "view_file",
                    "tool_info": {"name": "view_file", "parameters": {"AbsolutePath": target}}}})
evs.append({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 4, "step_type": "agent_response",
            "state": "DONE", "usage": {"input_tokens": 50, "cache_read_tokens": toks}}})
evs.append({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS", "response": "ok",
            "usage": {"input_tokens": toks + 50, "cache_read_tokens": toks}}})
for e in evs:
    print(json.dumps(e))
'''


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _exe(path, body):
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _skill(root, name, implicit=True, md=True, fm_disabled=False):
    d = os.path.join(root, name)
    os.makedirs(d)
    if md:
        with open(os.path.join(d, "SKILL.md"), "w") as fh:
            fh.write("---\nname: %s\ndescription: d\n%s---\nbody\n"
                     % (name, "disable-model-invocation: true\n" if fm_disabled else ""))
    if not implicit:
        os.makedirs(os.path.join(d, "agents"))
        with open(os.path.join(d, "agents", "openai.yaml"), "w") as fh:
            fh.write("policy:\n  allow_implicit_invocation: false\n")
    return d


class UnitTests(unittest.TestCase):
    def test_strip_mcp_tables(self):
        toml = ('model = "m1"\n[tui]\nx = 1\n[mcp_servers.a]\ncommand = "secret"\n'
                '[mcp_servers.a.env]\nK = "v"\n[mcp_servers."b"]\nurl = "u"\n[notice]\ny = 2\n')
        out = rc.strip_toml_tables(toml, "mcp_servers")
        self.assertNotIn("mcp_servers", out)
        self.assertNotIn("secret", out)
        self.assertNotIn('K = "v"', out)
        for keep in ('model = "m1"', "[tui]", "x = 1", "[notice]", "y = 2"):
            self.assertIn(keep, out)
        self.assertNotIn("mcp_servers", rc.strip_toml_tables('mcp_servers.a.command = "x"\nmodel = "m"\n', "mcp_servers"))

    def test_plan_and_paragraph(self):
        cat = {"a": "/s/a", "b": "/s/b", "c": "/s/c"}
        p = rc.plan_arms(cat, {"b", "c", "zz"}, "/store")
        self.assertEqual(p["listed"], {"visible": ["a", "b", "c"], "hidden": [], "index": None})
        self.assertEqual(p["hidden"]["visible"], ["a"])
        self.assertEqual(p["hidden"]["index"], None)
        self.assertEqual(p["index"]["hidden"], ["b", "c"])
        self.assertEqual(p["index"]["index"], "Other skills (not listed above). When one matches the task, "
                                              "read /store/<name>/SKILL.md first: b, c.")

    def test_detect_reads_real_shapes(self):
        texts = ["/bin/zsh -lc 'cat /tmp/arms/store/explain-branch/SKILL.md'",
                 'const r = await tools.exec_command({cmd:"cat /a/listed/skills/readback/SKILL.md",max_output_tokens:1});',
                 "sed -n '1,80p' /Users/x/.codex/skills/safe-git-sync/SKILL.md",
                 "ls /store/*/SKILL.md", "cat /store/<name>/SKILL.md", "cat notes.md",
                 "cat /tmp/arms/store/explain-branch/SKILL.md"]
        names, paths = rc.detect_reads(texts)
        self.assertEqual(names, ["explain-branch", "readback", "safe-git-sync"])
        self.assertEqual(len(paths), 3)
        self.assertEqual(rc.detect_reads(["echo hi", ""]), ([], []))

    def test_exclusion_and_invocable(self):
        with tempfile.TemporaryDirectory() as d:
            _skill(d, "ok")
            _skill(d, "dangling", md=False)
            _skill(d, "explicit-only", implicit=False)
            self.assertFalse(rc.is_excluded({"label": "positive", "skills": ["ok", "dangling"]}, d))
            self.assertTrue(rc.is_excluded({"label": "positive", "skills": ["dangling"]}, d))
            self.assertFalse(rc.is_excluded({"label": "negative", "skills": []}, d))
            self.assertTrue(rc.model_invocable(os.path.join(d, "ok")))
            self.assertFalse(rc.model_invocable(os.path.join(d, "dangling")))
            self.assertFalse(rc.model_invocable(os.path.join(d, "explicit-only")))

    def test_parse_prompt_input(self):
        items = [{"content": [{"text": "<skills_instructions>\n- `r0` = `/h/skills`\n- `r1` = `/h/skills/.system`\n"
                                       "- a: x (file: r0/a/SKILL.md)\n- s: y (file: r1/s/SKILL.md)"}]},
                 {"content": [{"text": "# AGENTS.md instructions\nfoo\n\nOther skills (not listed above). x"}]}]
        p = rc.CodexAdapter.parse_prompt_input(items)
        self.assertEqual((p["catalog"], p["system"], p["index_present"], p["agents_present"]), (["a"], ["s"], True, True))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.live = os.path.join(t, "live")
        self.skills = os.path.join(self.live, "skills")
        self.extra = os.path.join(t, "agents-skills")
        os.makedirs(self.skills)
        os.makedirs(self.extra)
        real = os.path.join(t, "real")
        os.makedirs(real)
        for n in ("keep", "deploy", "audit"):
            os.symlink(_skill(real, n), os.path.join(self.skills, n))
            os.symlink(os.path.join(real, n), os.path.join(self.extra, n))
        os.symlink(_skill(real, "explicit-only", implicit=False), os.path.join(self.skills, "explicit-only"))
        os.symlink(os.path.join(real, "gone"), os.path.join(self.skills, "gone"))  # dangling
        with open(os.path.join(self.live, "config.toml"), "w") as fh:
            fh.write('model = "m1"\n[mcp_servers.x]\ncommand = "do-not-copy"\n[notice]\ny = 1\n')
        with open(os.path.join(self.live, "AGENTS.override.md"), "w") as fh:
            fh.write("# rules\nbe brief\n")
        with open(os.path.join(self.live, "auth.json"), "w") as fh:
            fh.write("{}")
        self.settings = os.path.join(t, "settings.json")
        with open(self.settings, "w") as fh:
            json.dump({"skillOverrides": {"deploy": "name-only", "plug:audit": "name-only", "gone": "name-only",
                                          "keep": "on"}}, fh)
        self.codex = os.path.join(t, "codex")
        self.bun = os.path.join(t, "bun")
        _exe(self.codex, FAKE_CODEX)
        _exe(self.bun, FAKE_BUN)
        self.log = os.path.join(t, "bun.log")
        self.old = dict(os.environ)
        os.environ.update(FAKE_EXTRA_ROOT=self.extra, FAKE_BUN_LOG=self.log)
        self.root = os.path.join(t, "arms")
        self.common = ["--client", "codex", "--arms-root", self.root, "--overrides", self.settings,
                       "--live-home", self.live, "--codex-bin", self.codex, "--bun-bin", self.bun,
                       "--ach-ts", "/fake/ach.ts", "--skill-roots", self.skills, self.extra]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)
        self.tmp.cleanup()

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rc.main(list(args) + self.common)
        return code, out.getvalue(), err.getvalue()

    def test_build_check_run_report(self):
        code, out, err = self.main("build")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("FAIL", out)
        self.assertIn("listed  catalog=3 (+1 system) expected=3", out)
        self.assertIn("index   catalog=1 (+1 system) expected=1", out)
        man = rc.load_manifest(self.root)
        self.assertEqual(man["skipped_no_skill_md"], ["gone"])
        self.assertEqual(man["not_model_invocable"], ["explicit-only"])
        idx = os.path.join(self.root, "index")
        cfg = _read(os.path.join(idx, "config.toml"))
        self.assertNotIn("do-not-copy", cfg)
        self.assertIn('model = "m1"', cfg)
        self.assertIn('[[skills.config]]\nname = "deploy"\nenabled = false', cfg)
        self.assertTrue(os.path.islink(os.path.join(idx, "auth.json")))
        self.assertIn("read %s/<name>/SKILL.md first: audit, deploy." % man["store"], _read(os.path.join(idx, "AGENTS.md")))
        self.assertNotIn("Other skills", _read(os.path.join(self.root, "hidden", "AGENTS.md")))
        self.assertEqual(sorted(os.listdir(os.path.join(idx, "skills"))), ["keep"])
        # rebuild is idempotent
        self.assertEqual(self.main("build")[0], 0)

        cases = os.path.join(self.tmp.name, "cases.jsonl")
        with open(cases, "w") as fh:
            for c in ({"id": "p1", "label": "positive", "skills": ["deploy"], "prompt": "please deploy it"},
                      {"id": "p2", "label": "positive", "skills": ["gone"], "prompt": "please gone"},
                      {"id": "n1", "label": "negative", "skills": [], "prompt": "what is 2+2"}):
                fh.write(json.dumps(c) + "\n")
        res = os.path.join(self.tmp.name, "results.jsonl")
        code, _out, err = self.main("run", "--cases", cases, "--out", res, "--reps", "2")
        self.assertEqual(code, 0, err)
        recs = [json.loads(x) for x in _read(res).splitlines()]
        self.assertEqual(len(recs), 3 * 3 * 2)
        by = {r["key"]: r for r in recs}
        self.assertEqual(by["index|p1|0"]["skills"], ["deploy"])
        self.assertEqual(by["hidden|p1|0"]["skills"], [])
        self.assertTrue(by["listed|p2|1"]["excluded"])
        self.assertEqual(by["index|n1|0"]["skills"], [])
        # start tokens = the FIRST request from the rollout, not ach's tokens[0] (99999)
        self.assertEqual(by["hidden|n1|0"]["start_tokens_source"], "rollout")
        self.assertLess(by["hidden|n1|0"]["start_tokens"], by["index|n1|0"]["start_tokens"])
        calls = [json.loads(x) for x in _read(self.log).splitlines()]
        self.assertEqual(len(calls), 3 * 2 * 2)  # p2 excluded: no call
        self.assertEqual({c["codex_home"] for c in calls}, {os.path.join(self.root, a) for a in rc.ARMS})
        a0 = calls[0]["args"]
        self.assertEqual(a0[:4], ["/fake/ach.ts", "run", "--agent", "codex"])
        self.assertIn("--extra-args=--skip-git-repo-check --sandbox read-only", a0)
        self.assertEqual(len({c["cwd"] for c in calls}), len(calls))  # fresh cwd per run
        # resumable: nothing left to do
        code, _o, err = self.main("run", "--cases", cases, "--out", res, "--reps", "2")
        self.assertIn("0 runs to do", err)
        # report.py consumes the records unchanged
        rep = report.build_report(report.load(res), synthetic="include")
        self.assertEqual(rep["baseline"], "listed")
        self.assertEqual(rep["arms"]["index"]["recall"], (2, 2))
        self.assertEqual(rep["arms"]["hidden"]["recall"], (0, 2))
        self.assertIn("index", report.render(rep))


class AgyUnit(unittest.TestCase):
    def test_frontmatter_rule(self):
        with tempfile.TemporaryDirectory() as d:
            _skill(d, "ok")
            _skill(d, "off", fm_disabled=True)
            _skill(d, "yaml-off", implicit=False)  # codex's openai.yaml rule does not apply to agy
            _skill(d, "dangling", md=False)
            inv = rc.AgyAdapter.model_invocable
            self.assertEqual([inv(os.path.join(d, n)) for n in ("ok", "off", "yaml-off", "dangling")],
                             [True, False, True, False])
            with open(os.path.join(d, "ok", "SKILL.md"), "a") as fh:
                fh.write("disable-model-invocation: true\n")  # body text, not front matter
            self.assertTrue(inv(os.path.join(d, "ok")))

    def test_parse_stream_first_request_and_inputs_only(self):
        lines = [{"event": "init", "conversation_id": "c1"},
                 {"event": "step_update", "step_update": {"step_type": "agent_response", "usage": {
                     "input_tokens": 28287, "cache_read_tokens": 0}}},
                 {"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool",
                  "tool_info": {"parameters": {"AbsolutePath": "/a/listed/.gemini/config/skills/readback/SKILL.md"}}}},
                 {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool",
                  "tool_info": {"parameters": {"AbsolutePath": "/a/listed/.gemini/config/skills/readback/SKILL.md"},
                                "output": "see /b/other/SKILL.md"}}},
                 {"event": "step_update", "step_update": {"step_type": "agent_response", "usage": {
                     "input_tokens": 2056, "cache_read_tokens": 28310}}},
                 {"event": "result", "result": {"status": "SUCCESS", "usage": {"input_tokens": 30343}}}]
        out = "warning: noise\n" + "\n".join(json.dumps(x) for x in lines) + "\n"
        conv, first, texts, res = rc.AgyAdapter.parse_stream(out)
        self.assertEqual((conv, first, res["status"]), ("c1", 28287, "SUCCESS"))
        self.assertEqual(len(texts), 1)  # ACTIVE/DONE of one step counted once
        self.assertEqual(rc.detect_reads(texts)[0], ["readback"])  # tool output not scanned

    def test_prompt_catalog(self):
        t = ("<user_rules>x</user_rules><skills>\nintro\n- **SKILL.md** (required): x\n"
             "- a (/h/.gemini/config/skills/a/SKILL.md): d (with parens)\n"
             "- agy-guide (/h/.gemini/antigravity-cli/builtin/skills/agy_guide/SKILL.md): d\n</skills>"
             "<skills>\n- late (/h/x/late/SKILL.md): d\n</skills>")
        p = rc.AgyAdapter.prompt_catalog(t)
        self.assertEqual((p["catalog"], p["system"]), (["a"], ["agy-guide"]))


class AgyEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.live = os.path.join(t, "live-gemini")
        self.skills = os.path.join(t, "agents-skills")  # live ~/.gemini/config/skills -> ~/.agents/skills
        os.makedirs(os.path.join(self.live, "config"))
        os.makedirs(os.path.join(self.live, "antigravity-cli"))
        os.makedirs(self.skills)
        os.symlink(self.skills, os.path.join(self.live, "config", "skills"))
        real = os.path.join(t, "real")
        os.makedirs(real)
        for n in ("keep", "deploy", "audit"):
            os.symlink(_skill(real, n), os.path.join(self.skills, n))
        os.symlink(_skill(real, "manual", fm_disabled=True), os.path.join(self.skills, "manual"))
        os.symlink(os.path.join(real, "gone"), os.path.join(self.skills, "gone"))  # dangling
        with open(os.path.join(self.live, "config", "GEMINI.md"), "w") as fh:
            fh.write("# rules\nbe brief\n")
        with open(os.path.join(self.live, "config", "mcp_config.json"), "w") as fh:
            fh.write('{"mcpServers": {"x": {"serverUrl": "do-not-copy"}}}')
        with open(os.path.join(self.live, "config", "hooks.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(self.live, "antigravity-cli", "settings.json"), "w") as fh:
            json.dump({"model": "M1", "statusLine": {"command": "do-not-copy"}}, fh)
        self.keychains = os.path.join(t, "Keychains")
        os.makedirs(self.keychains)
        self.settings = os.path.join(t, "settings.json")
        with open(self.settings, "w") as fh:
            json.dump({"skillOverrides": {"deploy": "name-only", "plug:audit": "name-only", "gone": "name-only",
                                          "manual": "name-only", "keep": "on"}}, fh)
        self.agy = os.path.join(t, "agy")
        _exe(self.agy, FAKE_AGY)
        self.log = os.path.join(t, "agy.log")
        self.old = dict(os.environ)
        os.environ.update(FAKE_AGY_LOG=self.log)
        self.root = os.path.join(t, "arms")
        self.common = ["--client", "agy", "--arms-root", self.root, "--overrides", self.settings,
                       "--live-home", self.live, "--agy-bin", self.agy, "--keychains", self.keychains,
                       "--skill-roots", os.path.join(self.live, "config", "skills"), os.path.join(t, "no-agents")]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)
        self.tmp.cleanup()

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rc.main(list(args) + self.common)
        return code, out.getvalue(), err.getvalue()

    def test_build_check_break_run_report(self):
        code, out, err = self.main("build")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("FAIL", out)
        self.assertIn("listed  catalog=3 (+0 system) expected=3", out)
        self.assertIn("index   catalog=1 (+0 system) expected=1", out)
        man = rc.load_manifest(self.root)
        self.assertEqual(man["client"], "agy")
        self.assertEqual(man["skipped_no_skill_md"], ["gone"])
        self.assertEqual(man["not_model_invocable"], ["manual"])
        idx = os.path.join(self.root, "index")
        cfg = os.path.join(idx, ".gemini", "config")
        self.assertEqual(os.readlink(os.path.join(idx, "Library", "Keychains")), self.keychains)
        self.assertFalse(os.path.lexists(os.path.join(cfg, "mcp_config.json")))
        self.assertFalse(os.path.lexists(os.path.join(cfg, "hooks.json")))
        st = json.loads(_read(os.path.join(idx, ".gemini", "antigravity-cli", "settings.json")))
        self.assertEqual(st, {"permissions": {"allow": ["read_file(*)"]}, "model": "M1"})
        self.assertEqual(os.readlink(os.path.join(cfg, "AGENTS.md")), "GEMINI.md")
        self.assertIn("read %s/<name>/SKILL.md first: audit, deploy." % man["store"], _read(os.path.join(cfg, "GEMINI.md")))
        self.assertNotIn("Other skills", _read(os.path.join(self.root, "hidden", ".gemini", "config", "GEMINI.md")))
        self.assertEqual(sorted(os.listdir(os.path.join(cfg, "skills"))), ["keep"])
        # a broken arm (extra skill, or a stray ~/.agents/skills root) fails the check; build repairs it
        os.symlink(os.path.join(man["store"], "deploy"), os.path.join(self.root, "hidden", ".gemini", "config", "skills", "deploy"))
        os.makedirs(os.path.join(idx, ".agents", "skills"))
        os.symlink(os.path.join(man["store"], "audit"), os.path.join(idx, ".agents", "skills", "audit"))
        code, out, _err = self.main("check")
        self.assertEqual(code, 1)
        self.assertIn("extra=['deploy']", out)
        self.assertIn("extra=['audit']", out)
        code, out, _err = self.main("build")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.lexists(os.path.join(idx, ".agents")))

        cases = os.path.join(self.tmp.name, "cases.jsonl")
        with open(cases, "w") as fh:
            for c in ({"id": "p1", "label": "positive", "skills": ["deploy"], "prompt": "please deploy it"},
                      {"id": "p2", "label": "positive", "skills": ["gone"], "prompt": "please gone"},
                      {"id": "n1", "label": "negative", "skills": [], "prompt": "what is 2+2"}):
                fh.write(json.dumps(c) + "\n")
        res = os.path.join(self.tmp.name, "results.jsonl")
        code, _out, err = self.main("run", "--cases", cases, "--out", res, "--reps", "2", "--verify-prompt")
        self.assertEqual(code, 0, err)
        recs = [json.loads(x) for x in _read(res).splitlines()]
        self.assertEqual(len(recs), 3 * 3 * 2)
        by = {r["key"]: r for r in recs}
        self.assertEqual(by["listed|p1|0"]["skills"], ["deploy"])
        self.assertEqual(by["index|p1|0"]["skills"], ["deploy"])
        self.assertTrue(by["index|p1|0"]["read_paths"][0].startswith(man["store"]))
        self.assertEqual(by["hidden|p1|0"]["skills"], [])
        self.assertTrue(by["listed|p2|1"]["excluded"])
        self.assertEqual(by["index|n1|0"]["skills"], [])
        # the catalog the model received matches the plan; builtins are reported apart
        for arm in rc.ARMS:
            pc = by["%s|n1|0" % arm]["prompt_catalog"]
            self.assertEqual(pc["catalog"], sorted(man["plan"][arm]["visible"]))
            self.assertEqual(pc["system"], ["guide"])
        # start tokens = first agent_response input + cache read, never the whole-turn sum
        self.assertEqual(by["hidden|n1|0"]["start_tokens_source"], "stream-first-agent_response")
        self.assertLess(by["hidden|n1|0"]["start_tokens"], by["index|n1|0"]["start_tokens"])
        self.assertEqual(by["hidden|n1|0"]["start_tokens"] % 5, (1000 + 7) % 5)  # first request: toks + 7 cached
        calls = [json.loads(x) for x in _read(self.log).splitlines()]
        self.assertEqual(len(calls), 3 * 2 * 2)  # p2 excluded: no call
        self.assertEqual({c["home"] for c in calls}, {os.path.join(self.root, a) for a in rc.ARMS})
        a0 = calls[0]["args"]
        self.assertEqual(a0[0], "-p")
        self.assertEqual(a0[a0.index("--output-format") + 1], "stream-json")
        self.assertIn("--print-timeout", a0)
        self.assertNotIn("--dangerously-skip-permissions", a0)
        self.assertEqual(len({c["cwd"] for c in calls}), len(calls))  # fresh cwd per run
        code, _o, err = self.main("run", "--cases", cases, "--out", res, "--reps", "2")
        self.assertIn("0 runs to do", err)
        rep = report.build_report(report.load(res), synthetic="include")
        self.assertEqual(rep["arms"]["index"]["recall"], (2, 2))
        self.assertEqual(rep["arms"]["hidden"]["recall"], (0, 2))


if __name__ == "__main__":
    unittest.main()
