"""run_client.py OmpAdapter: arm building, rpc get_state check, session detector. No network."""
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

# Fake omp. `--mode rpc`: prints ready, answers get_state with a system prompt whose <skills>
# block lists customDirectories skills plus $FAKE_LEAK_ROOT skills unless enableClaudeUser is
# false (models the live-root leak), and a <file> for $PI_CODING_AGENT_DIR/AGENTS.md.
# `-p`: writes a session JSONL under --session-dir; "please X" loads skill://X when X is in the
# catalog, else reads <store>/X/SKILL.md via the index paragraph.
FAKE_OMP = r'''#!/usr/bin/env python3
import json, os, re, sys, uuid
a = sys.argv[1:]
root = os.environ["PI_CODING_AGENT_DIR"]
if os.environ.get("OMP_PROFILE"):
    sys.exit("profile set: agent dir override not honoured")
cfg = open(os.path.join(root, "config.yml")).read()
dirs = re.findall(r'^    - "?([^"\n]+)"?$', cfg, re.M)
roots = list(dirs)
if not re.search(r"enableClaudeUser: false", cfg):
    roots.append(os.environ["FAKE_LEAK_ROOT"])
cat = []
for r in roots:
    for n in sorted(os.listdir(r)):
        md = os.path.join(r, n, "SKILL.md")
        if os.path.isfile(md) and n not in cat and "disable-model-invocation: true" not in open(md).read():
            cat.append(n)
agents = os.path.join(root, "AGENTS.md")
text = open(agents).read() if os.path.exists(agents) else ""
log = os.environ.get("FAKE_OMP_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"args": a, "root": root, "cwd": os.getcwd()}) + "\n")
if "--mode" in a:
    sp = "<skills>\n" + "".join("- %s: d\n" % n for n in cat) + "</skills>\n"
    sp += '<file path="%s">\n%s\n</file>\n' % (agents, text)
    print(json.dumps({"type": "ready"}), flush=True)
    for line in sys.stdin:
        c = json.loads(line)
        if c.get("type") == "get_state":
            print(json.dumps({"id": c["id"], "type": "response", "command": "get_state", "success": True,
                              "data": {"model": {"id": "fake-m"}, "systemPrompt": [sp, "tail"]}}), flush=True)
    sys.exit(0)
prompt = a[-1]
sdir = a[a.index("--session-dir") + 1]
m = re.search(r"read (\S+)/<name>/SKILL\.md first", text)
w = re.search(r"please (\S+)", prompt)
calls = [{"type": "toolCall", "name": "glob", "arguments": {"path": "/x/*/SKILL.md"}}]
if w and w.group(1) in cat:
    calls.append({"type": "toolCall", "name": "read", "arguments": {"path": "skill://%s" % w.group(1)}})
elif w and m:
    calls.append({"type": "toolCall", "name": "read", "arguments": {"path": "%s/%s/SKILL.md" % (m.group(1), w.group(1))}})
toks = 1000 + 5 * len(text) + 7 * len(cat)
with open(os.path.join(sdir, "2026-10-17T00-00-00-000Z_%s.jsonl" % uuid.uuid4()), "w") as f:
    f.write(json.dumps({"type": "session", "id": "s"}) + "\n")
    f.write(json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}) + "\n")
    f.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": calls,
            "usage": {"input": toks - 100, "cacheRead": 100, "cacheWrite": 0, "output": 5}}}) + "\n")
    f.write(json.dumps({"type": "message", "message": {"role": "toolResult", "content": [{"type": "text",
            "text": "cat /store/zzz/SKILL.md"}]}}) + "\n")
    f.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input": 99999, "cacheRead": 0, "cacheWrite": 0, "output": 1}}}) + "\n")
print("ok")
'''


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _skill(root, name, invocable=True, desc=True):
    d = os.path.join(root, name)
    os.makedirs(d)
    with open(os.path.join(d, "SKILL.md"), "w") as fh:
        fh.write("---\nname: %s\n%s%s---\nbody\n" % (name, "description: d\n" if desc else "",
                                                     "" if invocable else "disable-model-invocation: true\n"))
    return d


class OmpUnit(unittest.TestCase):
    def test_strip_yaml_top_key(self):
        y = "providers:\n  webSearch: x\nskills:\n  enabled: true\n  customDirectories:\n    - /a\n\nmodelRoles:\n  default: m\n"
        out = rc.strip_yaml_top_key(y, "skills")
        self.assertNotIn("skills:", out)
        self.assertNotIn("/a", out)
        self.assertIn("providers:\n  webSearch: x\n", out)
        self.assertIn("modelRoles:\n  default: m\n", out)

    def test_model_invocable(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(rc.OmpAdapter.model_invocable(_skill(d, "ok")))
            self.assertFalse(rc.OmpAdapter.model_invocable(_skill(d, "manual", invocable=False)))
            self.assertFalse(rc.OmpAdapter.model_invocable(_skill(d, "nodesc", desc=False)))
            os.makedirs(os.path.join(d, "empty"))
            self.assertFalse(rc.OmpAdapter.model_invocable(os.path.join(d, "empty")))

    def test_parse_system_prompt(self):
        sp = ['x\n<skills>\n- a: one: two\n- b: d\n</skills>\n<file path="/r/AGENTS.md">\nfoo\n\n'
              'Other skills (not listed above). y\n</file>', "z"]
        p = rc.OmpAdapter.parse_system_prompt(sp, "/r")
        self.assertEqual((p["catalog"], p["index_present"], p["agents_present"]), (["a", "b"], True, True))
        self.assertFalse(rc.OmpAdapter.parse_system_prompt(sp, "/other")["agents_present"])
        self.assertEqual(rc.OmpAdapter.parse_system_prompt([], "/r")["catalog"], [])

    def test_read_session(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.jsonl")
            with open(p, "w") as fh:
                for e in ({"type": "session"},
                          {"type": "message", "message": {"role": "assistant", "usage": {"input": 10, "cacheRead": 5, "cacheWrite": 1},
                           "content": [{"type": "toolCall", "name": "read", "arguments": {"path": "skill://a/refs/x.md"}},
                                       {"type": "toolCall", "name": "bash", "arguments": {"command": "cat /s/b/SKILL.md"}}]}},
                          {"type": "message", "message": {"role": "toolResult", "content": [{"text": "skill://c"}]}},
                          {"type": "message", "message": {"role": "assistant", "usage": {"input": 999}, "content": []}}):
                    fh.write(json.dumps(e) + "\n")
            usage, texts, native = rc.OmpAdapter.read_session(p)
            self.assertEqual(rc.OmpAdapter.start_tokens(usage), 16)
            self.assertEqual(native, ["a"])
            self.assertEqual(rc.detect_reads(texts)[0], ["b"])


class OmpEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.live = os.path.join(t, "live")
        self.skills = os.path.join(self.live, "skills")
        self.extra = os.path.join(t, "claude-skills")
        self.leak = os.path.join(t, "leak")
        for d in (self.skills, self.extra, self.leak):
            os.makedirs(d)
        real = os.path.join(t, "real")
        os.makedirs(real)
        for n in ("keep", "deploy"):
            os.symlink(_skill(real, n), os.path.join(self.skills, n))
        os.symlink(_skill(real, "audit"), os.path.join(self.extra, "audit"))
        os.symlink(_skill(real, "manual", invocable=False), os.path.join(self.skills, "manual"))
        os.symlink(os.path.join(real, "gone"), os.path.join(self.skills, "gone"))  # dangling
        _skill(self.leak, "leaked")
        with open(os.path.join(self.live, "config.yml"), "w") as fh:
            fh.write("modelRoles:\n  default: prov/m1\nskills:\n  enableClaudeUser: true")  # no trailing newline
        with open(os.path.join(self.live, "AGENTS.md"), "w") as fh:
            fh.write("# rules\nbe brief\n")
        self.settings = os.path.join(t, "settings.json")
        with open(self.settings, "w") as fh:
            json.dump({"skillOverrides": {"deploy": "name-only", "plug:audit": "name-only", "gone": "name-only",
                                          "keep": "on"}}, fh)
        self.omp = os.path.join(t, "omp")
        with open(self.omp, "w") as fh:
            fh.write(FAKE_OMP)
        os.chmod(self.omp, os.stat(self.omp).st_mode | stat.S_IXUSR)
        self.log = os.path.join(t, "omp.log")
        self.old = dict(os.environ)
        os.environ.update(FAKE_LEAK_ROOT=self.leak, FAKE_OMP_LOG=self.log, OMP_PROFILE="work")
        self.root = os.path.join(t, "arms")
        self.common = ["--client", "omp", "--arms-root", self.root, "--overrides", self.settings,
                       "--live-home", self.live, "--omp-bin", self.omp, "--skill-roots", self.skills, self.extra]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)
        self.tmp.cleanup()

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = rc.main(list(args) + self.common)
        return code, out.getvalue(), err.getvalue()

    def test_build_check_tamper_run_report(self):
        code, out, err = self.main("build")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("FAIL", out)
        self.assertIn("listed  catalog=3 (+0 system) expected=3", out)
        self.assertIn("index   catalog=1 (+0 system) expected=1", out)
        man = rc.load_manifest(self.root)
        self.assertEqual(man["client"], "omp")
        self.assertEqual(man["skipped_no_skill_md"], ["gone"])
        self.assertEqual(man["not_model_invocable"], ["manual"])
        idx = os.path.join(self.root, "index")
        cfg = _read(os.path.join(idx, "config.yml"))
        self.assertIn("modelRoles:\n  default: prov/m1\nskills:\n", cfg)
        self.assertEqual(cfg.count("skills:"), 1)
        for k in rc.OMP_SKILL_TOGGLES:
            self.assertIn("  %s: false\n" % k, cfg)
        self.assertIn('    - "%s"\n' % os.path.join(idx, "skills"), cfg)
        self.assertFalse(os.path.lexists(os.path.join(idx, "agent.db")))
        self.assertIn("read %s/<name>/SKILL.md first: audit, deploy." % man["store"], _read(os.path.join(idx, "AGENTS.md")))
        self.assertNotIn("Other skills", _read(os.path.join(self.root, "hidden", "AGENTS.md")))
        self.assertEqual(sorted(os.listdir(os.path.join(idx, "skills"))), ["keep"])
        # profile env never reaches omp (it would redirect the agent dir)
        calls = [json.loads(x) for x in _read(self.log).splitlines()]
        self.assertEqual({c["root"] for c in calls}, {os.path.join(self.root, a) for a in rc.ARMS})

        # a tampered arm (catalog != plan) fails the check; a leaked live root does too
        os.symlink(os.path.realpath(os.path.join(self.skills, "deploy")), os.path.join(self.root, "hidden", "skills", "deploy"))
        code, out, _ = self.main("check")
        self.assertEqual(code, 1)
        self.assertIn("diff: missing=[] extra=['deploy']", out)
        self.assertEqual(self.main("build")[0], 0)  # rebuild is idempotent and repairs it
        hcfg = os.path.join(self.root, "hidden", "config.yml")
        with open(hcfg, "w") as fh:
            fh.write(_read(hcfg).replace("enableClaudeUser: false", "enableClaudeUser: true"))
        code, out, _ = self.main("check")
        self.assertEqual(code, 1)
        self.assertIn("extra=['leaked']", out)
        self.assertEqual(self.main("build")[0], 0)

        cases = os.path.join(self.tmp.name, "cases.jsonl")
        with open(cases, "w") as fh:
            for c in ({"id": "p1", "label": "positive", "skills": ["deploy"], "prompt": "please deploy it"},
                      {"id": "p2", "label": "positive", "skills": ["gone"], "prompt": "please gone"},
                      {"id": "n1", "label": "negative", "skills": [], "prompt": "what is 2+2"}):
                fh.write(json.dumps(c) + "\n")
        res = os.path.join(self.tmp.name, "results.jsonl")
        open(self.log, "w").close()
        code, _out, err = self.main("run", "--cases", cases, "--out", res, "--reps", "2", "--deadline", "60")
        self.assertEqual(code, 0, err)
        recs = [json.loads(x) for x in _read(res).splitlines()]
        self.assertEqual(len(recs), 3 * 3 * 2)
        by = {r["key"]: r for r in recs}
        self.assertEqual(by["listed|p1|0"]["skills"], ["deploy"])  # native skill:// load
        self.assertEqual(by["listed|p1|0"]["read_paths"], [])
        self.assertEqual(by["index|p1|0"]["skills"], ["deploy"])  # store SKILL.md read
        self.assertEqual(by["index|p1|0"]["read_paths"], [os.path.join(man["store"], "deploy", "SKILL.md")])
        self.assertEqual(by["hidden|p1|0"]["skills"], [])
        self.assertTrue(by["listed|p2|1"]["excluded"])
        self.assertEqual(by["index|n1|0"]["skills"], [])  # tool result text is not scanned
        # start tokens = the FIRST assistant usage incl. cache reads, not a later request (99999)
        h = by["hidden|n1|0"]
        self.assertEqual(h["start_tokens_source"], "session")
        self.assertEqual(h["start_tokens"], h["start_usage"]["input"] + h["start_usage"]["cacheRead"])
        self.assertLess(h["start_tokens"], by["index|n1|0"]["start_tokens"])
        self.assertLess(h["start_tokens"], by["listed|n1|0"]["start_tokens"])
        calls = [json.loads(x) for x in _read(self.log).splitlines()]
        self.assertEqual(len(calls), 3 * 2 * 2)  # p2 excluded: no call
        a0 = calls[0]["args"]
        self.assertEqual(a0[0], "-p")
        self.assertIn("--session-dir", a0)
        self.assertEqual(a0[a0.index("--max-time") + 1], "60")
        self.assertEqual(a0[a0.index("--tools") + 1], "read,grep,glob")
        self.assertEqual(len({c["cwd"] for c in calls}), len(calls))  # fresh cwd per run
        code, _o, err = self.main("run", "--cases", cases, "--out", res, "--reps", "2")
        self.assertIn("0 runs to do", err)
        rep = report.build_report(report.load(res), synthetic="include")
        self.assertEqual(rep["arms"]["index"]["recall"], (2, 2))
        self.assertEqual(rep["arms"]["hidden"]["recall"], (0, 2))


if __name__ == "__main__":
    unittest.main()
