import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import helpers
import _claude
import filter as filt
import make_arms
import mine
import report
import synth
import run

AB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M = "test-model-1"  # any explicit id; the bare alias "haiku" is rejected


class FakeClaudeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        helpers.make_fake_bin(self.dir)
        self.log = os.path.join(self.dir, "calls.log")
        self._old = dict(os.environ)
        os.environ["PATH"] = self.dir + os.pathsep + os.environ["PATH"]
        os.environ["FAKE_CLAUDE_LOG"] = self.log
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-should-be-stripped"
        os.environ.pop("FAKE_CLAUDE_HANG", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)
        self.tmp.cleanup()

    def calls(self):
        with open(self.log) as f:
            return [json.loads(x) for x in f if x.strip()]

    def p(self, name):
        return os.path.join(self.dir, name)


class MineTests(FakeClaudeCase):
    def test_mine(self):
        def u(text, **kw):
            d = {"type": "user", "message": {"role": "user", "content": text},
                 "timestamp": "2099-01-01T00:00:00Z"}
            d.update(kw)
            return d

        def a(skill):
            return {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Skill", "input": {"skill": skill}}]}}
        rows = [
            u("push the app live to staging"), a("deploy"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            u("run the deploy skill now"), a("deploy"), a("ship"),
            u("what time is it"),
            u("<command-name>/foo</command-name>"),
            u("caveat", isMeta=True),
            u("sidechain prompt", isSidechain=True),
            u("what time is it"),  # duplicate
            u("old prompt", timestamp="2000-01-01T00:00:00Z"),
            u("x" * 400),
        ]
        sub = os.path.join(self.dir, "proj")
        os.makedirs(sub)
        helpers.write_jsonl(os.path.join(sub, "s.jsonl"), rows)
        out = self.p("c.jsonl")
        self.assertEqual(mine.main([self.dir + "/proj", "--out", out, "--days", "36500"]), 0)
        got = _claude.read_jsonl(out)
        by = {r["prompt"]: r for r in got}
        self.assertEqual(by["push the app live to staging"]["skills"], ["deploy"])
        self.assertFalse(by["push the app live to staging"]["explicit"])
        self.assertTrue(by["run the deploy skill now"]["explicit"])
        self.assertEqual(by["run the deploy skill now"]["skills"], ["deploy", "ship"])
        self.assertEqual(by["what time is it"]["label"], "negative")
        self.assertEqual(sum(1 for r in got if r["prompt"] == "what time is it"), 1)
        for bad in ("sidechain prompt", "caveat", "x" * 400):
            self.assertNotIn(bad, by)
        self.assertFalse(any(r["prompt"].startswith("<") for r in got))
        self.assertEqual(len(got), 4)  # 2 positives + "what time" + "old prompt" (window is 100 years)

    def test_headless_skipped_by_default(self):
        helpers.write_jsonl(self.p("s.jsonl"), [
            {"type": "user", "entrypoint": "sdk-cli", "message": {"content": "a test case"},
             "timestamp": "2099-01-01T00:00:00Z"},
            {"type": "user", "entrypoint": "cli", "message": {"content": "a real ask"},
             "timestamp": "2099-01-01T00:00:00Z"}])
        out = self.p("c.jsonl")
        mine.main([self.p("s.jsonl"), "--out", out, "--days", "0"])
        self.assertEqual([r["prompt"] for r in _claude.read_jsonl(out)], ["a real ask"])
        mine.main([self.p("s.jsonl"), "--out", out, "--days", "0", "--include-headless"])
        self.assertEqual(len(_claude.read_jsonl(out)), 2)

    def test_days_window_drops_old(self):
        helpers.write_jsonl(self.p("s.jsonl"), [
            {"type": "user", "message": {"content": "old one"}, "timestamp": "2000-01-01T00:00:00Z"}])
        out = self.p("c.jsonl")
        mine.main([self.p("s.jsonl"), "--out", out, "--days", "30"])
        self.assertEqual(_claude.read_jsonl(out), [])


class FilterTests(FakeClaudeCase):
    def test_filter_keeps_and_strips_key(self):
        cands = [{"id": "c%d" % i, "prompt": "p%d" % i, "label": "negative", "skills": []} for i in range(30)]
        cands[3]["prompt"] = "yes"
        helpers.write_jsonl(self.p("c.jsonl"), cands)
        rc = filt.main([self.p("c.jsonl"), "--out", self.p("k.jsonl"), "--batch-size", "25", "--model", M, "--no-selftest"])
        self.assertEqual(rc, 0)
        kept = {c["id"] for c in _claude.read_jsonl(self.p("k.jsonl"))}
        self.assertEqual(len(kept), 29)
        self.assertNotIn("c3", kept)
        calls = self.calls()
        self.assertEqual(len(calls), 2)  # 30 cands / 25 per batch
        self.assertFalse(calls[0]["has_key"])
        self.assertEqual(calls[0]["max_out"], "2000")
        a = calls[0]["args"]
        self.assertEqual(a[a.index("--model") + 1], M)
        self.assertEqual(a[a.index("--tools") + 1], "")

    def test_filter_list_shaped_output(self):
        os.environ["FAKE_CLAUDE_LIST"] = "1"
        helpers.write_jsonl(self.p("c.jsonl"), [{"id": "c1", "prompt": "real ask", "label": "negative", "skills": []}])
        self.assertEqual(filt.main([self.p("c.jsonl"), "--out", self.p("k.jsonl"), "--model", M, "--no-selftest"]), 0)
        self.assertEqual(len(_claude.read_jsonl(self.p("k.jsonl"))), 1)

    def test_deadline_kills(self):
        os.environ["FAKE_CLAUDE_HANG"] = "1"
        t = time.time()
        with self.assertRaises(_claude.ClaudeError):
            _claude.run_claude(["-p", "x", "--output-format", "json"], deadline=1)
        self.assertLess(time.time() - t, 10)

    def test_parse_ids(self):
        self.assertEqual(filt.parse_ids('Sure: ["a","b"] done'), {"a", "b"})
        with self.assertRaises(ValueError):
            filt.parse_ids("none")


class SynthTests(FakeClaudeCase):
    def setUp(self):
        super().setUp()
        sd = self.p("skills")
        for n, extra in (("deploy", ""), ("haiku-poem", ""), ("hidden", "disable-model-invocation: true\n"), ("covered", "")):
            os.makedirs(os.path.join(sd, n))
            with open(os.path.join(sd, n, "SKILL.md"), "w") as f:
                f.write("---\nname: %s\ndescription: Use when asked about %s things.\n%s---\nb\n" % (n, n, extra))
        helpers.write_jsonl(self.p("cases.jsonl"), [
            {"id": "c1", "prompt": "real covered prompt", "label": "positive", "skills": ["covered"], "explicit": False}])

    def test_synth(self):
        rc = synth.main([self.p("cases.jsonl"), "--skills-dir", self.p("skills"), "--per-skill", "2", "--model", M, "--no-selftest"])
        self.assertEqual(rc, 0)
        rows = _claude.read_jsonl(self.p("cases.jsonl"))
        syn = [r for r in rows if r.get("synthetic")]
        self.assertEqual(sorted({r["skills"][0] for r in syn}), ["deploy", "haiku-poem"])  # not hidden, not covered
        self.assertEqual(len(syn), 4)
        for r in syn:
            self.assertEqual((r["label"], r["explicit"]), ("positive", False))
        self.assertEqual(len({r["id"] for r in syn}), 4)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0]["has_key"])
        self.assertEqual(calls[0]["args"][calls[0]["args"].index("--model") + 1], M)
        # idempotent: rerun adds nothing (dedupe) -- skills now have positives anyway
        synth.main([self.p("cases.jsonl"), "--skills-dir", self.p("skills"), "--model", M, "--no-selftest"])
        self.assertEqual(len(_claude.read_jsonl(self.p("cases.jsonl"))), 5)

    def test_dedupe_and_arm_skip(self):
        json.dump({"skillOverrides": {"deploy": "user-invocable-only"}}, open(self.p("a.json"), "w"))
        synth.main([self.p("cases.jsonl"), "--skills-dir", self.p("skills"), "--arms", self.p("a.json"), "--per-skill", "1", "--model", M, "--no-selftest"])
        names = {r["skills"][0] for r in _claude.read_jsonl(self.p("cases.jsonl")) if r.get("synthetic")}
        self.assertEqual(names, {"haiku-poem"})

    def test_min_positives_dry_run(self):
        synth.main([self.p("cases.jsonl"), "--skills-dir", self.p("skills"), "--min-positives", "2", "--dry-run", "--model", M, "--no-selftest"])
        self.assertFalse(os.path.exists(self.log))  # no model call in dry-run


class SelftestTests(FakeClaudeCase):
    def run_main(self, mod, argv):
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = mod.main(argv)
        return rc, err.getvalue()

    def cands(self):
        helpers.write_jsonl(self.p("c.jsonl"), [{"id": "c1", "prompt": "real ask", "label": "negative", "skills": []}])
        return [self.p("c.jsonl"), "--out", self.p("k.jsonl"), "--model", M]

    def test_fixture_shape(self):
        fx = _claude.read_jsonl(filt.FIXTURE)
        self.assertEqual(len(fx), 12)
        self.assertEqual(sorted({c["expect"] for c in fx}), ["drop", "keep"])

    def test_filter_selftest_pass(self):
        rc, err = self.run_main(filt, ["--selftest", "--model", M])
        self.assertEqual(rc, 0)
        self.assertIn("selftest PASS agreement 12/12 (need >= 10)", err)
        self.assertEqual(len(self.calls()), 1)

    def test_filter_selftest_fail_lists_disagreements(self):
        os.environ["FAKE_JUDGE"] = "bad"
        rc, err = self.run_main(filt, ["--selftest", "--model", M])
        self.assertEqual(rc, 1)
        self.assertIn("selftest FAIL agreement 0/12", err)
        self.assertEqual(err.count("disagree"), 12)

    def test_filter_selftest_threshold_flag(self):
        os.environ["FAKE_JUDGE"] = "weak"  # 9/12
        self.assertEqual(self.run_main(filt, ["--selftest", "--model", M])[0], 1)
        rc, err = self.run_main(filt, ["--selftest", "--selftest-min", "9", "--model", M])
        self.assertEqual(rc, 0)
        self.assertIn("agreement 9/12 (need >= 9)", err)
        os.environ["FAKE_JUDGE"] = "edge"  # 10/12 passes the default
        self.assertEqual(self.run_main(filt, ["--selftest", "--model", M])[0], 0)

    def test_filter_normal_run_selftest_first_and_counted(self):
        rc, err = self.run_main(filt, self.cands())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls()), 2)  # selftest + 1 batch
        self.assertIn("1 selftest calls", err)
        self.assertEqual(len(_claude.read_jsonl(self.p("k.jsonl"))), 1)

    def test_filter_abort_on_failed_selftest(self):
        os.environ["FAKE_JUDGE"] = "bad"
        rc, err = self.run_main(filt, self.cands())
        self.assertEqual(rc, 1)
        self.assertIn("ABORT: judge selftest failed", err)
        self.assertEqual(len(self.calls()), 1)  # only the selftest call
        self.assertFalse(os.path.exists(self.p("k.jsonl")))

    def test_filter_no_selftest_skips(self):
        os.environ["FAKE_JUDGE"] = "bad"
        rc, err = self.run_main(filt, self.cands() + ["--no-selftest"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("0 selftest calls", err)

    def synth_args(self):
        sd = self.p("skills")
        os.makedirs(os.path.join(sd, "deploy"))
        with open(os.path.join(sd, "deploy", "SKILL.md"), "w") as f:
            f.write("---\nname: deploy\ndescription: Use when asked about deploy things.\n---\nb\n")
        helpers.write_jsonl(self.p("cases.jsonl"), [])
        return [self.p("cases.jsonl"), "--skills-dir", sd, "--per-skill", "1", "--model", M]

    def test_synth_selftest_pass(self):
        rc, err = self.run_main(synth, ["--selftest", "--model", M])
        self.assertEqual(rc, 0)
        self.assertEqual(err.count("selftest PASS"), 4)
        self.assertNotIn("FAIL", err)

    def test_synth_selftest_fail_short_and_named(self):
        os.environ["FAKE_SYNTH"] = "short"
        rc, err = self.run_main(synth, ["--selftest", "--model", M])
        self.assertEqual(rc, 1)
        self.assertIn("selftest FAIL returned 3 prompts (got 2)", err)
        os.environ["FAKE_SYNTH"] = "named"
        rc, err = self.run_main(synth, ["--selftest", "--model", M])
        self.assertEqual(rc, 1)
        self.assertIn("selftest FAIL no prompt contains the skill name 'ledger-reconcile'", err)

    def test_synth_normal_run_counts_and_aborts(self):
        rc, err = self.run_main(synth, self.synth_args())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls()), 2)  # selftest + 1 skill
        self.assertIn("1 selftest calls", err)
        os.environ["FAKE_SYNTH"] = "short"
        os.remove(self.p("cases.jsonl")); helpers.write_jsonl(self.p("cases.jsonl"), [])
        n = len(self.calls())
        rc, err = self.run_main(synth, self.synth_args_again())
        self.assertEqual(rc, 1)
        self.assertIn("ABORT: generator selftest failed", err)
        self.assertEqual(len(self.calls()), n + 1)
        self.assertEqual(_claude.read_jsonl(self.p("cases.jsonl")), [])

    def synth_args_again(self):
        return [self.p("cases.jsonl"), "--skills-dir", self.p("skills"), "--per-skill", "1", "--model", M]

    def test_synth_no_selftest_skips(self):
        os.environ["FAKE_SYNTH"] = "short"
        rc, err = self.run_main(synth, self.synth_args() + ["--no-selftest"])
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("0 selftest calls", err)

    def test_cli_help_mentions_flags(self):
        for s in ("filter", "synth"):
            r = subprocess.run([sys.executable, os.path.join(AB, s + ".py"), "--help"], capture_output=True, text=True)
            self.assertIn("--selftest", r.stdout)
            self.assertIn("--no-selftest", r.stdout)


class ArmsTests(FakeClaudeCase):
    def make_skills(self):
        sd = self.p("skills")
        for n in ("deploy", "lint-thing"):
            os.makedirs(os.path.join(sd, n))
            with open(os.path.join(sd, n, "SKILL.md"), "w") as f:
                f.write("---\nname: %s\ndescription: x\n---\nbody\n" % n)
        return sd

    def test_arms(self):
        sd = self.make_skills()
        rc = make_arms.main(["--skills-dir", sd, "--out", self.p("arms"), "--arm", "before", "--arm", "after",
                             "--set", "after:lint-*=name-only"])
        self.assertEqual(rc, 0)
        before = json.load(open(self.p("arms/before.json")))["skillOverrides"]
        after = json.load(open(self.p("arms/after.json")))["skillOverrides"]
        self.assertEqual(open(self.p("arms/order.txt")).read().split(), ["before", "after"])
        self.assertEqual(before, {"deploy": "on", "lint-thing": "on"})
        self.assertEqual(after, {"deploy": "on", "lint-thing": "name-only"})

    def test_base_settings(self):
        sd = self.make_skills()
        with open(self.p("settings.json"), "w") as f:
            json.dump({"skillOverrides": {"lint-thing": "user-invocable-only", "gone": "off"}}, f)
        make_arms.main(["--skills-dir", sd, "--out", self.p("arms"), "--base-settings", self.p("settings.json"),
                        "--arm", "current", "--arm", "after", "--set", "after:deploy=name-only"])
        cur = json.load(open(self.p("arms/current.json")))["skillOverrides"]
        after = json.load(open(self.p("arms/after.json")))["skillOverrides"]
        self.assertEqual(cur, {"deploy": "on", "lint-thing": "user-invocable-only"})
        self.assertEqual(after, {"deploy": "name-only", "lint-thing": "user-invocable-only"})

    def test_bad_rule(self):
        with self.assertRaises(SystemExit):
            make_arms.parse_rule("after:x=bogus")


class RunReportTests(FakeClaudeCase):
    def setUp(self):
        super().setUp()
        helpers.write_jsonl(self.p("cases.jsonl"), [
            {"id": "c1", "prompt": "please deploy it", "label": "positive", "skills": ["deploy"], "explicit": False},
            {"id": "c2", "prompt": "deploy again now", "label": "positive", "skills": ["deploy"], "explicit": False},
            {"id": "c3", "prompt": "wrongfire hello", "label": "positive", "skills": ["deploy"], "explicit": False},
            {"id": "c4", "prompt": "what is 2+2", "label": "negative", "skills": []},
        ])
        os.makedirs(self.p("arms"))
        json.dump({"skillOverrides": {"deploy": "on"}}, open(self.p("arms/a_before.json"), "w"))
        open(self.p("arms/order.txt"), "w").write("zz_missing\na_before\nb_after\n")
        json.dump({"skillOverrides": {"deploy": "name-only"}}, open(self.p("arms/b_after.json"), "w"))

    def go(self, extra=()):
        return run.main(["--cases", self.p("cases.jsonl"), "--arms", self.p("arms"), "--out", self.p("res.jsonl"),
                         "--reps", "2", "--workers", "2", "--model", M] + list(extra))

    def test_run_resume_and_report(self):
        self.assertEqual(self.go(), 0)
        recs = _claude.read_jsonl(self.p("res.jsonl"))
        self.assertEqual(len(recs), 4 * 2 * 2)
        self.assertEqual(len(self.calls()), 16)
        for c in self.calls():
            self.assertFalse(c["has_key"])
            self.assertIn("--strict-mcp-config", c["args"])
            self.assertEqual(c["args"][c["args"].index("--tools") + 1:c["args"].index("--tools") + 5],
                             ["Skill", "Read", "Grep", "Glob"])
            self.assertEqual(c["args"][c["args"].index("--max-turns") + 1], "2")
        # resume: nothing new
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(_claude.read_jsonl(self.p("res.jsonl"))), 16)
        self.assertEqual(len(self.calls()), 16)
        # detection
        a = [r for r in recs if r["arm"] == "a_before" and r["case"] == "c1"]
        b = [r for r in recs if r["arm"] == "b_after" and r["case"] == "c1"]
        self.assertTrue(all(r["skills"] == ["deploy"] for r in a))
        self.assertTrue(all(r["skills"] == [] for r in b))
        self.assertEqual(a[0]["start_tokens"], 1105)
        # report
        rep = report.build_report(report.load(self.p("res.jsonl")))
        self.assertEqual(rep["baseline"], "a_before")
        self.assertEqual(rep["arms"]["a_before"]["recall"], (4, 6))   # c1,c2 hit x2; c3 miss
        self.assertEqual(rep["arms"]["b_after"]["recall"], (0, 6))
        self.assertEqual(rep["arms"]["a_before"]["wrong"], (2, 6))    # c3 fires 'other'
        self.assertEqual(rep["arms"]["a_before"]["neg_fire"], (0, 2))
        pr = rep["paired"]["b_after"]
        self.assertEqual((pr["cases"], pr["only_baseline"], pr["only_arm"]), (3, 2, 0))
        self.assertAlmostEqual(pr["p"], 0.5)
        self.assertEqual(pr["regressions"][0][0], "deploy")
        text = report.render(rep)
        self.assertIn("exact McNemar p = 0.5", text)
        sv = rep["savings"]["b_after"]  # fake claude reports identical tokens in both arms: all ties
        self.assertEqual((sv["cheaper"], sv["costlier"], sv["p"]), (0, 0, 1.0))
        self.assertAlmostEqual(sv["pct"], 0.0, places=9)
        self.assertIn("b_after start tokens vs a_before: +0.0%", text)

    def test_synthetic_excluded_by_default(self):
        with open(self.p("cases.jsonl"), "a") as f:
            f.write(json.dumps({"id": "s1", "prompt": "deploy synthetic one", "label": "positive",
                                "skills": ["deploy"], "explicit": False, "synthetic": True}) + "\n")
        self.go()
        recs = _claude.read_jsonl(self.p("res.jsonl"))
        self.assertTrue(all(r["synthetic"] for r in recs if r["case"] == "s1"))
        d = report.build_report(recs)
        self.assertEqual(d["arms"]["a_before"]["recall"], (4, 6))           # unchanged by synthetic
        self.assertEqual(d["arms"]["a_before"]["synthetic_recall"], (2, 2))
        self.assertEqual(report.build_report(recs, synthetic="include")["arms"]["a_before"]["recall"], (6, 8))
        self.assertEqual(report.build_report(recs, synthetic="only")["arms"]["a_before"]["recall"], (2, 2))
        self.assertIn("synthetic-only recall", report.render(d))

    def test_user_invocable_only_excluded(self):
        json.dump({"skillOverrides": {"deploy": "user-invocable-only"}}, open(self.p("arms/b_after.json"), "w"))
        self.go()
        recs = _claude.read_jsonl(self.p("res.jsonl"))
        ex = [r for r in recs if r.get("excluded")]
        self.assertEqual({r["arm"] for r in ex}, {"b_after"})
        self.assertEqual(len(ex), 6)  # 3 positives x 2 reps; no call made for them
        self.assertEqual(len(self.calls()), 16 - 6)
        rep = report.build_report(recs)
        self.assertEqual(rep["arms"]["b_after"]["recall"], (0, 0))
        self.assertEqual(rep["paired"]["b_after"]["cases"], 0)
        self.assertEqual(rep["paired"]["b_after"]["p"], 1.0)

    def test_error_retried_on_resume(self):
        os.environ["FAKE_CLAUDE_HANG"] = "1"
        self.assertEqual(self.go(["--deadline", "1", "--limit", "1", "--reps", "1", "--workers", "2"]), 1)
        recs = _claude.read_jsonl(self.p("res.jsonl"))
        self.assertTrue(all(r.get("error") for r in recs if not r.get("excluded")))
        del os.environ["FAKE_CLAUDE_HANG"]
        self.assertEqual(self.go(["--limit", "1", "--reps", "1"]), 0)
        good = [r for r in _claude.read_jsonl(self.p("res.jsonl")) if not r.get("error")]
        self.assertEqual(len(good), 2)
        rep = report.build_report(_claude.read_jsonl(self.p("res.jsonl")))
        self.assertEqual(rep["errors"], 2)  # historical error rows are counted but dropped

    def test_cli_help(self):
        for s in ("mine", "filter", "make_arms", "run", "report", "quick_ab"):
            r = subprocess.run([sys.executable, os.path.join(AB, s + ".py"), "--help"], capture_output=True)
            self.assertEqual(r.returncode, 0, s)

    def test_quick_ab(self):
        import quick_ab
        with open(self.p("p.tsv"), "w") as f:
            f.write("# c\ndeploy\tplease deploy it\ndeploy\tdeploy again\n")
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = quick_ab.main(["--prompts", self.p("p.tsv"), "--a", self.p("arms/a_before.json"),
                           "--b", self.p("arms/b_after.json"), "--reps", "2", "--model", M])
        self.assertEqual(rc, 0)
        self.assertNotIn("errored runs", buf.getvalue())
        self.assertIn("A deploy", buf.getvalue())
        self.assertIn("only A hit: 2, only B hit: 0, exact McNemar p = 0.5", buf.getvalue())

    def test_quick_ab_errored_runs_excluded(self):
        import quick_ab
        import io
        import contextlib
        with open(self.p("p.tsv"), "w") as f:
            f.write("deploy\tplease deploy it\ndeploy\tdeploy again\n")
        wrapper = self.p("flaky")  # arm B's settings file makes the binary exit nonzero
        with open(wrapper, "w") as f:
            f.write("#!/bin/sh\ncase \"$*\" in *b_after*) echo boom >&2; exit 1;; esac\n"
                    "exec %s \"$@\"\n" % os.path.join(self.dir, "claude"))
        os.chmod(wrapper, 0o755)
        out, errbuf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
            rc = quick_ab.main(["--prompts", self.p("p.tsv"), "--a", self.p("arms/a_before.json"),
                                "--b", self.p("arms/b_after.json"), "--reps", "2", "--model", M,
                                "--claude-bin", wrapper])
        o = out.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("errored runs: A 0, B 4 (excluded from the counts above)", o)
        self.assertIn("B deploy                   0/0 (2 err) (no ok runs, left out of McNemar)", o)
        # an arm with no ok runs must not read as "only A hit"
        self.assertIn("only A hit: 0, only B hit: 0", o)
        self.assertIn("prompts left out of McNemar (an arm had no ok runs): 2", o)
        self.assertIn("arm A: 4/4 runs hit", o)
        self.assertIn("arm B: 0/0 runs hit (Wilson95 n/a)", o)
        self.assertIn("error:", errbuf.getvalue())


if __name__ == "__main__":
    unittest.main()
