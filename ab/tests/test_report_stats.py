import json
import unittest

import helpers  # noqa: F401  (sets sys.path)
import report
from report import mcnemar_exact, wilson


def rec(arm, idx, case, rep, label="positive", expected=("deploy-helper",), skills=(), error=False,
        explicit=False):
    r = {"key": "%s|%s|%d" % (arm, case, rep), "arm": arm, "arm_idx": idx, "case": case, "rep": rep,
         "label": label, "expected": list(expected) if label == "positive" else [],
         "skills": list(skills), "explicit": explicit}
    if error:
        r["error"] = "boom"
    return r


def arm_recs(arm, idx, hits, extra=()):
    """hits: list of per-prompt lists of per-rep hit booleans."""
    out = []
    for i, reps in enumerate(hits):
        for j, h in enumerate(reps):
            out.append(rec(arm, idx, "p%d" % i, j, skills=["deploy-helper"] if h else []))
    return out + list(extra)


class CrossCheckFixtures(unittest.TestCase):
    def test_mcnemar_known_values(self):
        for b, c, p in [(1, 9, 0.021484375), (0, 5, 0.0625), (2, 8, 0.109375), (3, 3, 1.0), (0, 0, 1.0),
                        (0, 10, 0.001953125)]:
            self.assertAlmostEqual(mcnemar_exact(b, c), p, delta=1e-6)
            self.assertAlmostEqual(mcnemar_exact(c, b), p, delta=1e-6)

    def test_wilson_known_bounds(self):
        for k, n, lo, hi in [(0, 10, 0.0, 0.27753280), (10, 10, 0.72246720, 1.0),
                             (8, 10, 0.49016247, 0.94331785), (81, 263, 0.25528852, 0.36620958),
                             (1, 20, 0.00888145, 0.23613119)]:
            got = wilson(k, n)
            self.assertAlmostEqual(got[0], lo, delta=1e-6)
            self.assertAlmostEqual(got[1], hi, delta=1e-6)


class AggregationTests(unittest.TestCase):
    def recs(self):
        # p0: 2/3 hit, p1: 1/3 hit, p2: 3/3 hit
        return arm_recs("a", 0, [[1, 1, 0], [1, 0, 0], [1, 1, 1]])

    def test_rules_change_prompt_recall(self):
        want = {"majority": 2, "all": 1, "any": 3}
        for rule, k in want.items():
            rep = report.build_report(self.recs(), aggregate=rule)
            self.assertEqual(rep["arms"]["a"]["prompt"]["recall"], (k, 3), rule)
            self.assertEqual(rep["aggregate"], rule)

    def test_default_is_majority(self):
        self.assertEqual(report.build_report(self.recs())["aggregate"], "majority")

    def test_mcnemar_pairs_are_prompts_not_runs(self):
        base = arm_recs("a", 0, [[1, 1, 1]] * 4)
        cand = arm_recs("b", 1, [[0, 0, 0]] * 4)
        p = report.build_report(base + cand)["paired"]["b"]
        self.assertEqual((p["cases"], p["only_baseline"], p["only_arm"]), (4, 4, 0))
        self.assertAlmostEqual(p["p"], 0.125, delta=1e-9)  # 4 prompts, not 12 runs

    def test_negative_and_wrong_use_rule(self):
        recs = []
        for j, s in enumerate([["other"], ["other"], []]):
            recs.append(rec("a", 0, "n0", j, label="negative", skills=s))
            recs.append(rec("a", 0, "w0", j, skills=s))
        a = report.build_report(recs)["arms"]["a"]["prompt"]
        self.assertEqual(a["neg_fire"], (1, 1))
        self.assertEqual(a["wrong"], (1, 1))
        a = report.build_report(recs, aggregate="all")["arms"]["a"]["prompt"]
        self.assertEqual(a["neg_fire"], (0, 1))

    def test_tie_is_conservative_for_safety_metrics(self):
        # 1 of 2 reps: tied negative counts as fired, tied wrong pick counts as wrong, tied hit is a miss
        recs = []
        for j, s in enumerate([["other"], []]):
            recs.append(rec("a", 0, "n0", j, label="negative", skills=s))
            recs.append(rec("a", 0, "w0", j, skills=s))
        for j, s in enumerate([["deploy-helper"], []]):
            recs.append(rec("a", 0, "h0", j, skills=s))
        a = report.build_report(recs)["arms"]["a"]["prompt"]
        self.assertEqual(a["neg_fire"], (1, 1))
        self.assertEqual(a["wrong"], (1, 2))
        self.assertEqual(a["recall"], (0, 2))


class ExpectNoneAndReadSkillsTests(unittest.TestCase):
    def test_expect_none_row_is_negative(self):
        r = rec("a", 0, "n", 0, skills=["other"])
        r["expect_none"] = True
        r["expected"] = []
        p = report.build_report([r])["arms"]["a"]["prompt"]
        self.assertEqual(p["neg_fire"], (1, 1))
        self.assertEqual(p["recall"], (0, 0))
        self.assertEqual(p["wrong"], (0, 0))

    def test_read_skills_never_in_recall_and_shown_separately(self):
        recs = []
        for i in range(2):
            r = rec("a", 0, "p%d" % i, 0, skills=[])
            r["read_skills"] = ["deploy-helper"] if i == 0 else []
            recs.append(r)
        rep = report.build_report(recs)
        p = rep["arms"]["a"]["prompt"]
        self.assertEqual(p["recall"], (0, 2))
        self.assertEqual(p["read_fires"], (1, 2))
        self.assertEqual(rep["arms"]["a"]["recall"], (0, 2))
        self.assertIn("read-only", report.render(rep))


class NonInferiorityTests(unittest.TestCase):
    def pair(self, n_same, b, c, **kw):
        base = arm_recs("a", 0, [[1]] * n_same + [[1]] * b + [[0]] * c)
        cand = arm_recs("b", 1, [[1]] * n_same + [[0]] * b + [[1]] * c)
        return report.build_report(base + cand, **kw)["paired"]["b"]

    def test_non_inferior(self):
        p = self.pair(200, 0, 0)
        self.assertEqual(p["verdict"], "non-inferior")
        self.assertGreater(p["lower_pp"], -5)

    def test_zero_discordance_small_n_is_not_non_inferior(self):
        self.assertEqual(self.pair(3, 0, 0)["verdict"], "inconclusive")

    def test_inferior(self):
        p = self.pair(60, 40, 0)
        self.assertEqual(p["verdict"], "inferior")
        self.assertLess(p["diff_pp"], -30)

    def test_inconclusive_small_n(self):
        p = self.pair(8, 1, 0)
        self.assertEqual(p["verdict"], "inconclusive")

    def test_margin_flag_changes_verdict(self):
        self.assertEqual(self.pair(30, 1, 0, margin_pp=5)["verdict"], "inconclusive")
        self.assertEqual(self.pair(30, 1, 0, margin_pp=30)["verdict"], "non-inferior")

    def test_lower_bound_value(self):
        p = self.pair(90, 6, 4)  # n=100, d=-0.02, var=(11/100-0.0004), 11 = 10 discordant + 1 pseudo pair
        se = ((11 / 100 - 0.0004) ** 0.5) / 10
        self.assertAlmostEqual(p["diff_pp"], -2.0, delta=1e-9)
        self.assertAlmostEqual(p["lower_pp"], 100 * (-0.02 - 1.6448536269514722 * se), delta=1e-6)


class ErrorRateTests(unittest.TestCase):
    def test_error_rate_and_invalid(self):
        base = arm_recs("a", 0, [[1]] * 40)
        cand = arm_recs("b", 1, [[1]] * 40)
        cand += [rec("b", 1, "e%d" % i, 0, error=True) for i in range(3)]
        rep = report.build_report(base + cand)
        self.assertEqual(rep["arms"]["b"]["error_rate"], (3, 43))
        self.assertEqual(rep["paired"]["b"]["verdict"], "invalid")
        self.assertIn("invalid", report.render(rep))
        rep = report.build_report(base + cand, max_error_rate=0.2)
        self.assertEqual(rep["paired"]["b"]["verdict"], "non-inferior")

    def test_under_cap_not_invalid(self):
        base = arm_recs("a", 0, [[1]] * 40)
        cand = arm_recs("b", 1, [[1]] * 40) + [rec("b", 1, "e", 0, error=True)]
        rep = report.build_report(base + cand)
        self.assertNotEqual(rep["paired"]["b"]["verdict"], "invalid")

    def test_is_error_flag_counts(self):
        r = rec("a", 0, "x", 0)
        r["is_error"] = True
        rep = report.build_report([r, rec("a", 0, "y", 0, skills=["deploy-helper"])])
        self.assertEqual(rep["arms"]["a"]["error_rate"], (1, 2))


class EntryTableTests(unittest.TestCase):
    def test_per_entry_and_render(self):
        recs = []
        for arm, idx, hit in (("a", 0, True), ("b", 1, False)):
            for i in range(3):
                recs.append(rec(arm, idx, "d%d" % i, 0, expected=["deploy-helper"],
                                skills=["deploy-helper"] if hit else []))
                recs.append(rec(arm, idx, "t%d" % i, 0, expected=["team:dev:context"],
                                skills=["team:dev:context"]))
        rep = report.build_report(recs)
        e = rep["entries"]
        self.assertEqual(e["deploy-helper"]["arms"]["a"], [3, 3])
        self.assertEqual(e["deploy-helper"]["arms"]["b"], [0, 3])
        self.assertAlmostEqual(e["deploy-helper"]["delta_pp"]["b"], -100.0, delta=1e-9)
        self.assertEqual(e["team:dev:context"]["n"], 3)
        text = report.render(rep)
        self.assertIn("deploy-helper", text)
        self.assertIn("team:dev:context", text)
        json.dumps(rep)

    def test_cli_flags(self):
        import contextlib
        import io
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.jsonl")
            with open(p, "w") as fh:
                for r in arm_recs("a", 0, [[1, 0]] * 3) + arm_recs("b", 1, [[1, 1]] * 3):
                    fh.write(json.dumps(r) + "\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(report.main([p, "--aggregate", "any", "--margin-pp", "10",
                                              "--max-error-rate", "0.1"]), 0)


if __name__ == "__main__":
    unittest.main()
