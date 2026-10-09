import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import make_rename_arms as mra  # noqa: E402
import run  # noqa: E402


class RenameArmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.skills = os.path.join(self.tmp, "skills")
        os.makedirs(os.path.join(self.skills, "boil-the-ocean"))
        with open(os.path.join(self.skills, "boil-the-ocean", "SKILL.md"), "w") as fh:
            fh.write("---\nname: boil-the-ocean\ndescription: go all in\n---\nbody\n")
        self.base = os.path.join(self.tmp, "base.json")
        with open(self.base, "w") as fh:
            json.dump({"skillOverrides": {"boil-the-ocean": "name-only", "other": "on"}}, fh)
        self.ren = os.path.join(self.tmp, "ren.json")
        with open(self.ren, "w") as fh:
            json.dump({"boil-the-ocean": "going-all-in"}, fh)
        self.out = os.path.join(self.tmp, "arms")

    def make(self, *extra):
        return mra.main(["--skills-dir", self.skills, "--base", self.base, "--rename", self.ren,
                         "--name", "renamed", "--out", self.out] + list(extra))

    def test_writes_arm_meta_and_skill_copy(self):
        self.assertEqual(self.make(), 0)
        ov = json.load(open(os.path.join(self.out, "renamed.json")))["skillOverrides"]
        self.assertEqual(ov["boil-the-ocean"], "off")
        self.assertEqual(ov["going-all-in"], "name-only")
        self.assertEqual(ov["other"], "on")
        meta = json.load(open(os.path.join(self.out, "renamed.meta.json")))
        self.assertEqual(meta["rename"], {"going-all-in": "boil-the-ocean"})
        text = open(os.path.join(meta["cwd"], ".claude", "skills", "going-all-in", "SKILL.md")).read()
        self.assertIn("name: going-all-in\n", text)
        self.assertNotIn("name: boil-the-ocean", text)

    def test_prefix_and_bad_name(self):
        self.assertEqual(self.make("--prefix", "use-this-skill-when-"), 0)
        meta = json.load(open(os.path.join(self.out, "renamed.meta.json")))
        self.assertIn("use-this-skill-when-going-all-in", meta["rename"])
        self.assertEqual(self.make("--prefix", "Bad_"), 2)

    def test_run_maps_names_back_and_does_not_exclude(self):
        self.make()
        arm = os.path.join(self.out, "renamed.json")
        meta = run.load_meta(arm)
        self.assertEqual(run.unrename(["going-all-in", "plug:other"], meta), ["boil-the-ocean", "plug:other"])
        case = {"label": "positive", "skills": ["boil-the-ocean"]}
        self.assertFalse(run.is_excluded(case, arm))  # original is off, but its new name is listed
        self.assertEqual(run.load_meta(self.base), {})


if __name__ == "__main__":
    unittest.main()
