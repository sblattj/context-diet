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


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _exe(path, body):
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _skill(root, name, implicit=True, md=True):
    d = os.path.join(root, name)
    os.makedirs(d)
    if md:
        with open(os.path.join(d, "SKILL.md"), "w") as fh:
            fh.write("---\nname: %s\ndescription: d\n---\nbody\n" % name)
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


if __name__ == "__main__":
    unittest.main()
