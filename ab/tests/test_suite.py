import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402  (first: puts ab/ on sys.path)
import _claude  # noqa: E402
import suite  # noqa: E402
from test_pipeline import FakeClaudeCase  # noqa: E402

M = "test-model-1"
AB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tree_digest(root):
    h = hashlib.sha256()
    for d, dirs, files in sorted(os.walk(root)):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, root).encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


class SplitTests(unittest.TestCase):
    def test_convert_both_schemas(self):
        self.assertEqual(suite.convert({"id": "a", "prompt": "x", "kind": "negative", "expected": None})["skills"],
                         ["none"])
        c = suite.convert({"id": "b", "prompt": "x", "kind": "positive", "expected": "deploy",
                           "expected_any": ["release"]})
        self.assertEqual((c["label"], c["skills"], c["expected_any"]), ("positive", ["release", "deploy"],
                                                                         ["release", "deploy"]))
        kit = {"id": "c", "prompt": "x", "label": "positive", "skills": ["review-pr"]}
        self.assertEqual(suite.convert(kit), kit)

    def test_expected_as_list(self):
        c = suite.convert({"id": "c", "prompt": "x", "kind": "positive", "expected": ["deploy", "ship"]})
        self.assertEqual(c["label"], "positive")
        self.assertEqual(c["skills"], ["deploy", "ship"])

    def test_split_by_skills_and_tags(self):
        splits = [("starters", {"deploy"}, set()), ("tagged", set(), {"slow"})]
        self.assertEqual(suite.split_of({"skills": ["DEPLOY"]}, splits), "starters")
        self.assertEqual(suite.split_of({"expected": "review-pr", "tags": ["slow"]}, splits), "tagged")
        self.assertEqual(suite.split_of({"expected": "review-pr"}, splits), "headline")
        self.assertEqual(suite.plan_calls({"headline": 5, "starters": 2}, 3, 2, 2, 1), (3 + 2) * 2 * 2)
        self.assertEqual(suite.plan_calls({"headline": 5, "starters": 2}, 0, 1, 1, 2), 14)


class SuiteTests(FakeClaudeCase):
    def setUp(self):
        super().setUp()
        for k in (suite.KIT_ENV, suite.SNAPSHOT_ENV):
            os.environ.pop(k, None)
        self.ws = self.p("ws")
        for n in ("deploy", "review-pr"):
            os.makedirs(os.path.join(self.ws, ".claude", "skills", n))
            with open(os.path.join(self.ws, ".claude", "skills", n, "SKILL.md"), "w") as fh:
                fh.write("---\nname: %s\ndescription: does %s\n---\nbody\n" % (n, n))
        os.makedirs(os.path.join(self.ws, ".claude", "commands", "team"))
        with open(os.path.join(self.ws, ".claude", "commands", "team", "ship.md"), "w") as fh:
            fh.write("Ship the current branch.\n")
        rows = [{"id": "d%d" % i, "prompt": "please deploy thing %d" % i, "kind": "positive",
                 "expected": "deploy"} for i in range(3)]
        rows += [{"id": "r0", "prompt": "look at my change", "kind": "positive", "expected": "review-pr"},
                 {"id": "n0", "prompt": "rename a variable", "kind": "negative", "expected": None},
                 {"id": "m0", "prompt": "and deploy it too", "kind": "positive", "expected": "deploy",
                  "prior_turns": ["earlier"]}]
        helpers.write_jsonl(self.p("cases.jsonl"), rows)
        with open(self.p("split.json"), "w") as fh:
            json.dump({"starters": ["deploy"]}, fh)

    def suite(self, *extra, out="out"):
        argv = ["--workspace", self.ws, "--cases", self.p("cases.jsonl"), "--out", self.p(out),
                "--model", M, "--reps", "1", "--split", self.p("split.json")] + list(extra)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return suite.main(argv)

    def manifest(self, out="out"):
        with open(self.p(out + "/manifest.json")) as fh:
            return json.load(fh)

    def test_dry_run_splits_plans_and_builds_every_arm(self):
        with open(self.p("profiles.json"), "w") as fh:
            json.dump({"stock": {"claude_args": ["--setting-sources", "project,local"], "env": {"X": 1}},
                       "plain": {}}, fh)
        before = tree_digest(self.ws)
        rc = self.suite("--arm", "baseline,control,control-on,desc-blank", "--profiles", self.p("profiles.json"),
                        "--reps", "2", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.log), "dry run makes no model calls")
        m = self.manifest()
        c = m["case_counts"]
        self.assertEqual(c["total"], 6)
        self.assertEqual(c["splits"], {"headline": 2, "starters": 3})
        self.assertEqual(c["skipped_multi_turn"], ["m0"])
        self.assertRegex(c["cases_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(m["planned_calls"], 5 * 2 * 4 * 2)
        self.assertEqual(m["arms"], ["baseline", "control", "control-on", "desc-blank"])
        for k in ("kit_sha", "workspace_sha", "start_time", "end_time", "runner_sha256"):
            self.assertIn(k, m)
        heads = _claude.read_jsonl(self.p("out/cases.headline.jsonl"))
        self.assertFalse(any("deploy" in r["skills"] for r in heads))
        arms = self.p("out/stock/arms")
        ctl = suite.read_json(os.path.join(arms, "control.json"))["skillOverrides"]
        self.assertEqual(ctl["deploy"], "off")
        self.assertIn("name-only", ctl.values())
        on = suite.read_json(os.path.join(arms, "control-on.json"))["skillOverrides"]
        self.assertTrue(any(k.startswith("skill-") and v == "on" for k, v in on.items()), on)
        blank = suite.read_json(os.path.join(arms, "desc-blank.json"))["skillOverrides"]
        self.assertEqual(blank, {"deploy": "name-only", "review-pr": "name-only", "team:ship": "name-only"})
        cmeta = suite.read_json(os.path.join(arms, "control.meta.json"))
        self.assertTrue(cmeta["control"])
        self.assertEqual(cmeta["claude_args"], ["--setting-sources", "project,local"])
        self.assertEqual(cmeta["env"], {"X": "1"})
        self.assertNotEqual(cmeta["cwd"], os.path.realpath(self.ws))
        self.assertNotIn("claude_args", suite.read_json(self.p("out/plain/arms/baseline.meta.json")))
        self.assertEqual(tree_digest(self.ws), before, "workspace is only read")

    def test_run_reports_and_resumes(self):
        self.assertEqual(self.suite(), 0)
        n_calls = len(self.calls())
        self.assertEqual(n_calls, 5 * 2)
        recs = _claude.read_jsonl(self.p("out/results-starters.jsonl"))
        base = [r for r in recs if r["arm"] == "baseline"]
        ctl = [r for r in recs if r["arm"] == "control"]
        self.assertTrue(all(r["skills"] == ["deploy"] for r in base))
        self.assertTrue(all(r["control"] and r["skills"] == [] for r in ctl), "control recall collapses")
        rep = suite.read_json(self.p("out/report-starters.json"))
        self.assertEqual(rep["baseline"], "baseline")
        self.assertTrue(os.path.getsize(self.p("out/report-headline.txt")) > 0)
        # resume: finished keys are skipped
        self.assertEqual(self.suite(), 0)
        self.assertEqual(len(self.calls()), n_calls)
        self.assertEqual(self.manifest()["runs"], 2)
        # an errored key is retried, and counts once in the error rate
        bad = dict(base[0], error="boom", is_error=True, skills=[])
        helpers.write_jsonl(self.p("out/results-starters.jsonl"),
                            [bad if r["key"] == bad["key"] else r for r in recs])
        self.assertEqual(suite.error_rate(self.p("out/results-starters.jsonl"), "baseline"), (3, 1))
        self.assertEqual(self.suite(), 0)
        self.assertEqual(len(self.calls()), n_calls + 1)
        self.assertEqual(suite.error_rate(self.p("out/results-starters.jsonl"), "baseline"), (3, 0))

    def test_error_rate_stops_the_run(self):
        os.environ["FAKE_CLAUDE_HANG"] = "1"
        rc = self.suite("--deadline", "0.5", "--workers", "5", "--arm", "baseline")
        self.assertEqual(rc, 4)
        m = self.manifest()
        self.assertIn("error rate over 20%", m["aborted"])
        self.assertIn("end_time", m)
        self.assertFalse(os.path.exists(self.p("out/results-starters.jsonl")), "stopped after the first split")

    def test_guards(self):
        self.assertEqual(self.suite("--refuse", self.ws, "--dry-run"), 3)
        self.assertEqual(self.suite("--dry-run", out="ws/out"), 3)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out")))
        with self.assertRaises(SystemExit):
            self.suite("--arm", "baseline,nope", "--dry-run")
        with self.assertRaises(SystemExit):
            self.suite("--model", "haiku", "--dry-run")

    def test_custom_arm_file(self):
        os.makedirs(self.p("mine"))
        with open(self.p("mine/slim.json"), "w") as fh:
            json.dump({"skillOverrides": {"review-pr": "name-only"}}, fh)
        self.assertEqual(self.suite("--arm", "baseline," + self.p("mine/slim.json")), 0)
        recs = _claude.read_jsonl(self.p("out/results-headline.jsonl"))
        self.assertEqual(sorted({r["arm"] for r in recs}), ["baseline", "slim"])

    def test_cli_runs_from_a_private_snapshot(self):
        r = subprocess.run([sys.executable, os.path.join(AB, "suite.py"), "--workspace", self.ws, "--cases",
                            self.p("cases.jsonl"), "--out", self.p("out"), "--model", M, "--dry-run"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("planned model calls = 20", r.stdout)
        m = self.manifest()
        self.assertTrue(m["runner_snapshot"])
        self.assertNotEqual(os.path.realpath(m["runner_snapshot"]), os.path.realpath(AB))
        self.assertFalse(os.path.exists(m["runner_snapshot"]), "snapshot removed on exit")
        self.assertEqual(os.path.realpath(m["kit"]), os.path.realpath(AB))
        with open(os.path.join(AB, "suite.py"), "rb") as fh:
            self.assertEqual(m["runner_sha256"], hashlib.sha256(fh.read()).hexdigest())


if __name__ == "__main__":
    unittest.main()
