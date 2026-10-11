import math
import unittest

import helpers  # noqa: F401  (sets sys.path)
import report
from report import build_report, case_medians, render, savings, sign_test_exact


def rec(arm, case, tok, idx=0, **kw):
    r = {"arm": arm, "arm_idx": idx, "case": case, "label": "positive", "expected": ["s"],
         "skills": ["s"], "start_tokens": tok}
    r.update(kw)
    return r


class SavingsMathTests(unittest.TestCase):
    def test_case_median_over_reps_skips_missing(self):
        recs = [rec("a", "c1", 10), rec("a", "c1", 30), rec("a", "c1", 20),
                rec("a", "c2", None), rec("a", "c2", 8)]
        self.assertEqual(case_medians(recs), {"c1": 20, "c2": 8})

    def test_known_ratios_exact_geomean(self):
        base = {"c1": 100, "c2": 100}
        arm = {"c1": 50, "c2": 200}  # ratios 0.5, 2.0 -> geo-mean 1.0
        s = savings(base, arm)
        self.assertAlmostEqual(s["geo_ratio"], 1.0, places=12)
        self.assertAlmostEqual(s["pct"], 0.0, places=9)
        s = savings({"a": 100, "b": 100, "c": 100}, {"a": 50, "b": 25, "c": 100 * 0.2})
        self.assertAlmostEqual(s["geo_ratio"], (0.5 * 0.25 * 0.2) ** (1 / 3), places=12)
        self.assertAlmostEqual(s["pct"], 100 * ((0.025) ** (1 / 3) - 1), places=9)

    def test_ci_contains_estimate_and_is_seed_deterministic(self):
        base = {"c%d" % i: 1000 for i in range(12)}
        arm = {"c%d" % i: 1000 * r for i, r in enumerate(
            [0.5, 0.6, 0.7, 0.8, 0.9, 1.1, 0.65, 0.75, 0.55, 0.95, 0.85, 0.7])}
        a, b, c = savings(base, arm, seed=3), savings(base, arm, seed=3), savings(base, arm, seed=4)
        self.assertEqual(a, b)
        self.assertLessEqual(a["ci"][0], a["pct"])
        self.assertLessEqual(a["pct"], a["ci"][1])
        self.assertLess(a["ci"][0], a["ci"][1])
        self.assertNotEqual(a["ci"], c["ci"])

    def test_sign_test_hand_computed(self):
        self.assertAlmostEqual(sign_test_exact(1, 10), 0.021484375, places=12)
        self.assertAlmostEqual(sign_test_exact(10, 10), 2 / 1024, places=12)
        self.assertEqual(sign_test_exact(0, 0), 1.0)
        self.assertEqual(sign_test_exact(5, 10), 1.0)
        # 3 cheaper, 1 costlier, 1 tie -> n=4, k=1: 2*(1+4)/16 = 0.625
        s = savings({c: 100 for c in "abcde"}, {"a": 50, "b": 60, "c": 70, "d": 150, "e": 100})
        self.assertEqual((s["cheaper"], s["costlier"], s["cases"]), (3, 1, 5))
        self.assertAlmostEqual(s["p"], 0.625, places=12)

    def test_fewer_than_two_pairs_is_none(self):
        self.assertIsNone(savings({"a": 1}, {"a": 1}))
        self.assertIsNone(savings({"a": 1, "b": 2}, {"c": 1, "d": 2}))
        self.assertIsNone(savings({}, {}))


class SavingsReportTests(unittest.TestCase):
    def recs(self, ncases):
        out = []
        for i in range(ncases):
            c = "c%d" % i
            for rep_tok in (1000, 1100, 900):  # median 1000
                out.append(rec("base", c, rep_tok, 0))
            for rep_tok in (500, 520, 480):  # median 500
                out.append(rec("slim", c, rep_tok, 1))
        out.append(rec("slim", "cerr", 5, 1, error="boom"))  # errored runs ignored
        out.append(rec("base", "cerr", 5, 0, error="boom"))
        return out

    def test_end_to_end(self):
        rep = build_report(self.recs(4))
        s = rep["savings"]["slim"]
        self.assertEqual((s["cases"], s["cheaper"], s["costlier"]), (4, 4, 0))
        self.assertAlmostEqual(s["pct"], -50.0, places=9)
        self.assertAlmostEqual(s["p"], 0.125, places=12)
        self.assertEqual(rep, build_report(self.recs(4)))
        text = render(rep)
        self.assertIn("slim start tokens vs base: -50.0% [-50.0, -50.0] "
                      "(geo-mean, 95% bootstrap, 4 cases); cheaper on 4/4, sign p = 0.12", text)

    def test_na_path(self):
        rep = build_report(self.recs(1))
        self.assertEqual(rep["savings"]["slim"], {"paired_cases": 1})
        self.assertIn("slim start tokens vs base: n/a (1 paired cases)", render(rep))


if __name__ == "__main__":
    unittest.main()
