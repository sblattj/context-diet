import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import make_rename_arms as mra  # noqa: E402
import run  # noqa: E402


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def snapshot(root):
    out = {}
    for r, _, names in os.walk(root):
        for n in names:
            p = os.path.join(r, n)
            out[p] = open(p).read()
    return out


class ControlCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.claude = os.path.join(self.tmp, "home", ".claude")
        self.skills = os.path.join(self.claude, "skills")
        put(os.path.join(self.skills, "deploy-helper", "SKILL.md"),
            "---\nname: deploy-helper\ndescription: ship it\n---\nbody\n")
        cmds = os.path.join(self.claude, "commands")
        put(os.path.join(cmds, "team", "dev", "context.md"),
            "---\ndescription: load the\n  team context\nmodel: x\n---\nDo it\n")
        put(os.path.join(cmds, "team", "dev", "plan.md"), "---\ndescription: plan\n---\nPlan\n")
        put(os.path.join(cmds, "solo.md"), "no frontmatter\n")
        self.base = os.path.join(self.tmp, "base.json")
        put(self.base, json.dumps({"skillOverrides": {"deploy-helper": "name-only"}}))
        self.out = os.path.join(self.tmp, "arms")
        self.before = snapshot(self.claude)

    def make(self, *extra):
        return mra.main(["--skills-dir", self.skills, "--base", self.base, "--control",
                         "--out", self.out] + list(extra))

    def meta(self):
        return json.load(open(os.path.join(self.out, "control.meta.json")))

    def test_nested_commands_renamed_and_meta_correct(self):
        self.assertEqual(self.make(), 0)
        meta = self.meta()
        cr = meta["command_rename"]
        self.assertEqual(sorted(cr), ["solo", "team:dev:context", "team:dev:plan"])
        self.assertEqual(cr["team:dev:context"], "team:dev:cmd-02")
        self.assertEqual(cr["team:dev:plan"], "team:dev:cmd-03")
        self.assertEqual(cr["solo"], "cmd-01")
        base = os.path.join(meta["cwd"], ".claude", "commands")
        self.assertTrue(os.path.exists(os.path.join(base, "team", "dev", "cmd-02.md")))
        self.assertTrue(os.path.exists(os.path.join(base, "team", "dev", "cmd-03.md")))
        self.assertTrue(os.path.exists(os.path.join(base, "cmd-01.md")))
        self.assertFalse(os.path.exists(os.path.join(base, "team", "dev", "context.md")))
        self.assertIn("Do it", open(os.path.join(base, "team", "dev", "cmd-02.md")).read())
        # existing keys kept, skills still renamed
        self.assertEqual(meta["rename"]["skill-01"], "deploy-helper")
        self.assertTrue(meta["control"])
        ov = json.load(open(os.path.join(self.out, "control.json")))["skillOverrides"]
        self.assertEqual(ov["team:dev:context"], "off")
        self.assertEqual(ov["team:dev:cmd-02"], "name-only")

    def test_unrename_maps_full_command_names(self):
        self.make()
        meta = self.meta()
        self.assertEqual(run.unrename(["team:dev:cmd-02", "skill-01", "cmd-01"], meta),
                         ["team:dev:context", "deploy-helper", "solo"])

    def test_command_positive_not_excluded_in_control_arm(self):
        # control arm turns the original name off and exposes the renamed one; a positive
        # expecting the original must resolve through command_rename, not be excluded
        self.make()
        arm = os.path.join(self.out, "control.json")
        case = {"label": "positive", "skills": ["team:dev:context"]}
        self.assertFalse(run.is_excluded(case, arm))

    def test_original_tree_untouched(self):
        self.make("--control-strip-descriptions")
        self.assertEqual(snapshot(self.claude), self.before)

    def test_strip_descriptions(self):
        self.make("--control-strip-descriptions")
        cwd = self.meta()["cwd"]
        c = open(os.path.join(cwd, ".claude", "commands", "team", "dev", "cmd-02.md")).read()
        self.assertIn('description: ""', c)
        self.assertNotIn("team context", c)
        self.assertIn("model: x", c)
        s = open(os.path.join(cwd, ".claude", "skills", "skill-01", "SKILL.md")).read()
        self.assertNotIn("ship it", s)
        self.assertIn('description: ""', s)

    def test_descriptions_kept_without_flag_and_no_commands_dir_ok(self):
        self.make()
        s = open(os.path.join(self.meta()["cwd"], ".claude", "skills", "skill-01", "SKILL.md")).read()
        self.assertIn("ship it", s)
        import shutil
        shutil.rmtree(os.path.join(self.claude, "commands"))
        self.assertEqual(self.make(), 0)
        self.assertEqual(self.meta()["command_rename"], {})

    def rename_args(self, mapping, *extra):
        rf = os.path.join(self.tmp, "ren.json")
        put(rf, json.dumps(mapping))
        return mra.main(["--skills-dir", self.skills, "--base", self.base, "--rename", rf,
                         "--name", "renamed", "--out", self.out] + list(extra))

    def err(self, argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                rc = mra.main(argv)
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    def test_strip_requires_control(self):
        rc, msg = self.err(["--skills-dir", self.skills, "--base", self.base, "--rename", self.base,
                            "--name", "x", "--out", self.out, "--control-strip-descriptions"])
        self.assertEqual(rc, 2)
        self.assertIn("requires --control", msg)

    def test_plain_rename_keeps_existing_cwd_commands(self):
        cwd = os.path.join(self.tmp, "w")
        put(os.path.join(cwd, ".claude", "commands", "keep.md"), "keep\n")
        self.assertEqual(self.rename_args({"deploy-helper": "ship-it"}, "--cwd", cwd), 0)
        self.assertTrue(os.path.exists(os.path.join(cwd, ".claude", "commands", "keep.md")))

    def test_control_without_commands_keeps_existing_cwd_commands(self):
        import shutil
        shutil.rmtree(os.path.join(self.claude, "commands"))
        cwd = os.path.join(self.tmp, "w")
        put(os.path.join(cwd, ".claude", "commands", "keep.md"), "keep\n")
        self.assertEqual(self.make("--cwd", cwd), 0)
        self.assertTrue(os.path.exists(os.path.join(cwd, ".claude", "commands", "keep.md")))

    def test_missing_explicit_commands_dir_exits_2(self):
        for extra in (["--control"], []):
            rf = os.path.join(self.tmp, "ren.json")
            put(rf, json.dumps({"deploy-helper": "ship-it"}))
            argv = ["--skills-dir", self.skills, "--base", self.base, "--out", self.out,
                    "--commands-dir", os.path.join(self.tmp, "nope")]
            argv += extra or ["--rename", rf, "--name", "r"]
            rc, _ = self.err(argv)
            self.assertEqual(rc, 2)

    def test_commands_dir_requires_command_mode(self):
        rc, msg = self.err(["--skills-dir", self.skills, "--base", self.base, "--out", self.out,
                            "--commands-dir", os.path.join(self.claude, "commands")])
        self.assertEqual(rc, 2)
        self.assertIn("--commands-dir requires", msg)

    def test_rename_arm_renames_commands(self):
        cwd = os.path.join(self.tmp, "w")
        put(os.path.join(cwd, ".claude", "commands", "stale.md"), "stale\n")
        self.assertEqual(self.rename_args({"deploy-helper": "ship-it",
                                           "team:dev:context": "load-team-context"}, "--cwd", cwd), 0)
        meta = json.load(open(os.path.join(self.out, "renamed.meta.json")))
        self.assertEqual(meta["command_rename"], {"team:dev:context": "team:dev:load-team-context"})
        self.assertNotIn("control", meta)
        base = os.path.join(cwd, ".claude", "commands")
        self.assertIn("Do it", open(os.path.join(base, "team", "dev", "load-team-context.md")).read())
        self.assertFalse(os.path.exists(os.path.join(base, "team", "dev", "plan.md")))
        ov = json.load(open(os.path.join(self.out, "renamed.json")))["skillOverrides"]
        self.assertEqual(ov["team:dev:context"], "off")
        self.assertEqual(ov["team:dev:load-team-context"], "name-only")
        self.assertEqual(run.unrename(["team:dev:load-team-context", "ship-it"], meta),
                         ["team:dev:context", "deploy-helper"])

    def test_rename_unknown_key_exits_2(self):
        self.assertEqual(self.rename_args({"nothing:here": "x"}), 2)


if __name__ == "__main__":
    unittest.main()
