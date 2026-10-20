import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers  # noqa: E402  (first: puts ab/ on sys.path)
import _claude  # noqa: E402
import make_rename_arms as mra  # noqa: E402
import report  # noqa: E402
import run  # noqa: E402
from test_pipeline import FakeClaudeCase  # noqa: E402

M = "test-model-1"


class ControlArmTests(FakeClaudeCase):
    def setUp(self):
        super().setUp()
        self.skills = self.p("skills")
        for n in ("deploy", "zeta-skill"):
            os.makedirs(os.path.join(self.skills, n))
            with open(os.path.join(self.skills, n, "SKILL.md"), "w") as fh:
                fh.write("---\nname: %s\ndescription: does %s\n---\nbody\n" % (n, n))
        os.makedirs(self.p("arms"))
        json.dump({"skillOverrides": {"deploy": "on", "zeta-skill": "on"}}, open(self.p("arms/base.json"), "w"))
        open(self.p("arms/order.txt"), "w").write("base\n")
        helpers.write_jsonl(self.p("cases.jsonl"), [
            {"id": "c%d" % i, "prompt": "please deploy thing %d" % i, "label": "positive",
             "skills": ["deploy"], "explicit": False} for i in range(6)])

    def make(self, *extra):
        return mra.main(["--skills-dir", self.skills, "--base", self.p("arms/base.json"),
                         "--control", "--out", self.p("arms")] + list(extra))

    def test_generation(self):
        self.assertEqual(self.make(), 0)
        ov = json.load(open(self.p("arms/control.json")))["skillOverrides"]
        self.assertEqual(ov, {"deploy": "off", "zeta-skill": "off", "skill-01": "name-only", "skill-02": "name-only"})
        meta = json.load(open(self.p("arms/control.meta.json")))
        self.assertTrue(meta["control"])
        self.assertEqual(meta["rename"], {"skill-01": "deploy", "skill-02": "zeta-skill"})
        text = open(os.path.join(meta["cwd"], ".claude", "skills", "skill-01", "SKILL.md")).read()
        self.assertIn("name: skill-01\n", text)
        self.assertEqual(self.make("--name", "ctl2"), 0)
        self.assertTrue(os.path.exists(self.p("arms/ctl2.json")))

    def test_control_state_on_keeps_descriptions_visible(self):
        # names-only control: opaque names but listed descriptions, so loss measures name sensitivity alone
        self.assertEqual(self.make("--state", "on", "--name", "ctlon"), 0)
        ov = json.load(open(self.p("arms/ctlon.json")))["skillOverrides"]
        self.assertEqual(ov, {"deploy": "off", "zeta-skill": "off", "skill-01": "on", "skill-02": "on"})
        self.assertTrue(json.load(open(self.p("arms/ctlon.meta.json")))["control"])
        self.assertEqual(self.make(), 0)
        ov = json.load(open(self.p("arms/control.json")))["skillOverrides"]
        self.assertEqual(ov["skill-01"], "name-only", "default control stays name-only")

    def test_requires_rename_without_control(self):
        with self.assertRaises(SystemExit):
            mra.main(["--skills-dir", self.skills, "--base", self.p("arms/base.json"), "--name", "x",
                      "--out", self.p("arms")])

    def test_run_calls_control_and_report_labels(self):
        self.make()
        case = _claude.read_jsonl(self.p("cases.jsonl"))[0]
        self.assertFalse(run.is_excluded(case, self.p("arms/control.json")))
        # control: turning the same skills off WITHOUT renaming is excluded (the trap this design avoids)
        json.dump({"skillOverrides": {"deploy": "off"}}, open(self.p("arms/off.json"), "w"))
        self.assertTrue(run.is_excluded(case, self.p("arms/off.json")))
        self.assertEqual(run.main(["--cases", self.p("cases.jsonl"), "--arms", self.p("arms"), "--out",
                                   self.p("res.jsonl"), "--reps", "1", "--model", M]), 0)
        recs = _claude.read_jsonl(self.p("res.jsonl"))
        ctl = [r for r in recs if r["arm"] == "control"]
        self.assertEqual(len(ctl), 6)
        self.assertFalse(any(r.get("excluded") for r in ctl))
        self.assertTrue(all(r.get("control") for r in ctl))
        self.assertFalse(any(r.get("control") for r in recs if r["arm"] == "base"))
        self.assertEqual(len(self.calls()), 6 + 6)  # control arm made real calls
        rep = report.build_report(recs)
        self.assertEqual(rep["baseline"], "base")
        self.assertEqual(rep["controls"], ["control"])
        self.assertTrue(rep["arms"]["control"]["control"])
        self.assertNotIn("control", rep["arms"]["base"])
        cal = rep["calibration"]["control"]
        self.assertEqual((cal["recall"], cal["baseline_recall"]), ([0, 6], [6, 6]))
        self.assertTrue(cal["detects_loss"])  # 6 discordant, p = 0.03125
        text = report.render(rep)
        self.assertIn("control [control]", text)
        self.assertIn("control control: recall 0% vs baseline 100% (harness can detect loss: yes", text)
        json.dumps(rep)

    def test_not_detected_when_no_loss(self):
        recs = []
        for arm, idx, ctl in (("base", 0, False), ("ctl", 1, True)):
            for i in range(6):
                r = {"key": "%s|c%d|0" % (arm, i), "arm": arm, "arm_idx": idx, "case": "c%d" % i, "rep": 0,
                     "label": "positive", "expected": ["deploy"], "skills": ["deploy"], "explicit": False}
                if ctl:
                    r["control"] = True
                recs.append(r)
        rep = report.build_report(recs)
        self.assertFalse(rep["calibration"]["ctl"]["detects_loss"])
        self.assertIn("harness can detect loss: no", report.render(rep))
        # CLI-named control works on records without the flag
        for r in recs:
            r.pop("control", None)
        self.assertEqual(report.build_report(recs, controls=["ctl"])["controls"], ["ctl"])
        self.assertEqual(report.build_report(recs)["controls"], [])


if __name__ == "__main__":
    unittest.main()
