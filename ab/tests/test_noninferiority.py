"""noninferiority.py: paired (case, rep) verdict, hidden split, floor exclusion, pre-registration."""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest

import helpers  # noqa: F401  (sets sys.path)
import noninferiority as ni


def row(arm, case, rep, label, expected, skills, tokens=1000):
    return {"arm": arm, "case": case, "rep": rep, "label": label, "expected": expected,
            "skills": skills, "start_tokens": tokens}


def dataset(arm_miss=0, arm_wrong_hidden=0, cases=40, reps=2, negatives=20):
    """Baseline hits every positive; the arm misses the first arm_miss cases and fires a wrong skill
    on the first arm_wrong_hidden hidden-only positives. 10 hidden-only positives, 10 mixed."""
    base, arm = [], []
    for r in range(reps):
        for i in range(cases):
            c = "c%02d" % i
            exp = ["alpha"] if i >= 10 else ["alpha", "hid:one"]
            base.append(row("base", c, r, "positive", exp, ["alpha"], 1000))
            arm.append(row("slim", c, r, "positive", exp, [] if i < arm_miss else ["alpha"], 700))
        for i in range(10):
            c = "h%02d" % i
            base.append(row("base", c, r, "positive", ["hid:one"], ["hid:one"], 1000))
            arm.append(row("slim", c, r, "positive", ["hid:one"], ["beta"] if i < arm_wrong_hidden else [], 700))
        for i in range(negatives):
            c = "n%02d" % i
            base.append(row("base", c, r, "negative", [], [], 1000))
            arm.append(row("slim", c, r, "negative", [], [], 700))
    return base, arm


class Workdir:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        cmd = os.path.join(self.d, "commands", "hid")
        os.makedirs(cmd)
        with open(os.path.join(cmd, "one.md"), "w") as fh:
            fh.write("---\ndescription: x\ndisable-model-invocation: true\n---\nbody\n")
        with open(os.path.join(self.d, "commands", "open.md"), "w") as fh:
            fh.write("---\ndescription: y\n---\nbody\n")
        self.commands = os.path.join(self.d, "commands")

    def jsonl(self, name, recs):
        p = os.path.join(self.d, name)
        with open(p, "w") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
        return p

    def text(self, name, body):
        p = os.path.join(self.d, name)
        with open(p, "w") as fh:
            fh.write(body)
        return p


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ni.main(argv + ["--json"])
    return code, json.loads(buf.getvalue())


class NonInferiorityTests(unittest.TestCase):
    def setUp(self):
        self.w = Workdir()
        self.addCleanup(self.w.tmp.cleanup)

    def files(self, **kw):
        b, a = dataset(**kw)
        return self.w.jsonl("base.jsonl", b), self.w.jsonl("arm.jsonl", a)

    def test_hidden_detection(self):
        self.assertEqual(ni.hidden_from_commands(self.w.commands), {"hid:one"})

    def test_keep(self):
        b, a = self.files()
        code, v = run([b, a, "--commands-dir", self.w.commands])
        self.assertEqual(code, 0)
        self.assertTrue(v["keep"])
        self.assertEqual(v["reasons"], [])
        m = v["metrics"]
        self.assertEqual(m["recall_visible"]["n"], 80)  # 40 cases x 2 reps; hidden-only positives dropped
        self.assertEqual(m["recall_mixed"]["n"], 20)
        self.assertEqual(m["recall_pure_visible"]["n"], 60)
        self.assertEqual(m["wrong_pick"]["n"], 100)
        self.assertEqual(m["false_fire"]["n"], 40)
        self.assertEqual(m["recall_visible"]["delta_pp"], 0.0)
        self.assertLess(m["recall_visible"]["ci_pp"][0], 0)  # pseudo-discordant floor, never a zero-width CI
        self.assertEqual(v["hidden"], ["hid:one"])
        self.assertAlmostEqual(v["savings"]["pct"], -30.0, places=6)

    def test_without_hidden_the_hidden_only_rows_count_against_recall(self):
        b, a = self.files()
        code, v = run([b, a])
        self.assertEqual(code, 1)
        self.assertEqual(v["metrics"]["recall_visible"]["n"], 100)
        self.assertTrue(any(r.startswith("recall_visible") for r in v["reasons"]))

    def test_park_on_recall_bound(self):
        b, a = self.files(arm_miss=6)  # 12 of 80 rows lost, -15 pp
        code, v = run([b, a, "--commands-dir", self.w.commands])
        self.assertEqual(code, 1)
        self.assertFalse(v["keep"])
        rv = v["metrics"]["recall_visible"]
        self.assertAlmostEqual(rv["delta_pp"], -15.0)
        self.assertLess(rv["ci_pp"][0], -5.0)
        self.assertEqual([r.split(":")[0] for r in v["reasons"]], ["recall_visible"])

    def test_small_loss_inside_margin_keeps(self):
        b, a = self.files(arm_miss=1)  # -2.5 pp, but clustered: 1 case of 40
        code, v = run([b, a, "--commands-dir", self.w.commands, "--margin-pp", "10"])
        self.assertTrue(v["keep"], v)

    def test_park_on_wrong_pick(self):
        b, a = self.files(arm_wrong_hidden=10)
        code, v = run([b, a, "--commands-dir", self.w.commands])
        self.assertEqual(code, 1)
        self.assertTrue(v["metrics"]["recall_visible"]["pass"])
        self.assertFalse(v["metrics"]["wrong_pick"]["pass"])
        self.assertGreater(v["metrics"]["wrong_pick"]["ci_pp"][0], 0)
        self.assertEqual([r.split(":")[0] for r in v["reasons"]], ["wrong_pick"])
        # dropping wrong_pick from the decision set turns it into information only
        code, v = run([b, a, "--commands-dir", self.w.commands, "--decide", "recall_visible,false_fire"])
        self.assertEqual(code, 0)
        self.assertFalse(v["metrics"]["wrong_pick"]["decides"])

    def test_interleaved_arm_file_matches_two_files(self):
        base, arm = dataset(arm_miss=6)
        mixed = [r for pair in zip(base, arm) for r in pair]
        one = self.w.jsonl("both.jsonl", mixed)
        b, a = self.files(arm_miss=6)
        _, v1 = run([one, "--commands-dir", self.w.commands])
        _, v2 = run([b, a, "--commands-dir", self.w.commands])
        self.assertEqual((v1["base_arm"], v1["arm"]), ("base", "slim"))
        self.assertEqual(v1["metrics"], v2["metrics"])
        self.assertEqual(v1["keep"], v2["keep"])
        # a custom arm field
        for r in mixed:
            r["variant"] = r.pop("arm")
        one = self.w.jsonl("both2.jsonl", mixed)
        _, v3 = run([one, "--arm-field", "variant", "--base-arm", "base", "--arm", "slim",
                     "--commands-dir", self.w.commands])
        self.assertEqual(v3["metrics"], v2["metrics"])
        # three arms without names is an error
        mixed.append(dict(mixed[0], variant="third"))
        one = self.w.jsonl("both3.jsonl", mixed)
        code, v4 = run([one, "--arm-field", "variant"])
        self.assertEqual(code, 2)
        self.assertFalse(v4["keep"])

    def test_floor_exclusion(self):
        b, a = self.files(arm_miss=6)
        floor = self.w.text("floor.txt", "# frozen floor\n" + "".join("c%02d\tknown flaky\n" % i for i in range(6)))
        code, v = run([b, a, "--commands-dir", self.w.commands, "--exclude-cases", floor])
        self.assertEqual(code, 0, v)
        self.assertEqual(v["metrics"]["recall_visible"]["n"], 68)
        self.assertEqual(v["excluded_rows"], 12)
        self.assertEqual(len(v["excluded_cases"]), 6)

    def test_errored_rows_dropped_last_good_row_wins(self):
        base, arm = dataset()
        arm.append(dict(arm[0], skills=[], error="timeout"))  # a later error does not replace the good row
        b, a = self.w.jsonl("b.jsonl", base), self.w.jsonl("a.jsonl", arm)
        _, v = run([b, a, "--commands-dir", self.w.commands])
        self.assertEqual(v["metrics"]["recall_visible"]["arm"], 80)

    def test_preregistration_sha(self):
        b, a = self.files()
        body = json.dumps({"alpha": 0.05, "sides": 2, "metrics": {"recall_visible": {"margin_pp": 5},
                                                                  "wrong_pick": {}, "false_fire": {}},
                           "hidden": ["hid:one"]})
        rule = self.w.text("rule.json", body)
        sha = hashlib.sha256(body.encode()).hexdigest()
        code, v = run([b, a, "--rule", rule, "--rule-sha256", sha])
        self.assertEqual(code, 0, v)
        self.assertEqual(v["rule_sha256"], sha)
        self.assertEqual(v["rule"]["metrics"]["recall_visible"]["margin_pp"], 5.0)
        # the rule is edited after the data: hard failure
        self.w.text("rule.json", body.replace('"margin_pp": 5', '"margin_pp": 50'))
        code, v = run([b, a, "--rule", rule, "--rule-sha256", sha])
        self.assertEqual(code, 2)
        self.assertFalse(v["keep"])
        self.assertIn("rule changed", v["reasons"][0])
        # flags cannot override what the rule fixes
        self.w.text("rule.json", body)
        code, v = run([b, a, "--rule", rule, "--margin-pp", "50"])
        self.assertEqual(code, 2)
        self.assertIn("margin_pp", v["reasons"][0])
        code, v = run([b, a, "--rule", rule, "--hidden", "other"])
        self.assertEqual(code, 2)

    def test_cluster_widens_ci_over_reps(self):
        rows = [("c%d" % (i // 3), 1, 0 if i < 9 else 1) for i in range(60)]  # 3 lost cases x 3 reps
        z = 1.959963984540054
        clus = ni.metric_stats(rows, "higher", 5.0, z, "case")
        flat = ni.metric_stats(rows, "higher", 5.0, z, "none")
        self.assertEqual(clus["delta_pp"], flat["delta_pp"])
        self.assertLess(clus["ci_pp"][0], flat["ci_pp"][0])

    def test_text_render(self):
        b, a = self.files(arm_miss=6)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ni.main([b, a, "--commands-dir", self.w.commands])
        out = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("PARK: slim vs base"))
        self.assertIn("park: recall_visible", out)
        self.assertIn("median start tokens: base 1000, slim 700", out)


if __name__ == "__main__":
    unittest.main()
