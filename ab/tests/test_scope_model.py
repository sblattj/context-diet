import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

import helpers
import filter as filt
import mine
import quick_ab
import run
import synth

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
import ccpaths  # noqa: E402


def quiet(fn, argv):
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            rc = fn(argv)
        except SystemExit as e:
            rc = e.code
    return rc, err.getvalue()


class ModelRequiredTests(unittest.TestCase):
    CASES = (
        (filt.main, ["c.jsonl", "--out", "k.jsonl"]),
        (synth.main, ["c.jsonl", "--skills-dir", "s"]),
        (run.main, ["--cases", "c.jsonl", "--arms", "a.json", "--out", "r.jsonl"]),
        (quick_ab.main, ["--prompts", "p.tsv", "--a", "a.json", "--b", "b.json"]),
    )

    def test_missing_model_is_usage_error(self):
        for fn, argv in self.CASES:
            rc, err = quiet(fn, argv)
            self.assertEqual(rc, 2, fn.__module__)
            self.assertIn("--model", err)

    def test_bare_haiku_alias_rejected(self):
        for fn, argv in self.CASES:
            for alias in ("haiku", "Haiku"):
                rc, err = quiet(fn, argv + ["--model", alias])
                self.assertEqual(rc, 2, fn.__module__)
                self.assertIn("explicit model id", err)

    def test_other_values_pass_through(self):
        for v in ("some-model-id-1", "sonnet", "x-haiku-y"):
            self.assertEqual(run.build_args("p", "a.json", v)[-1], v)
            self.assertEqual(filt._claude.model_arg(v), v)


class MineScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "projects")
        self.here = os.path.realpath(os.path.join(self.tmp.name, "work", "here.proj"))
        self.there = os.path.realpath(os.path.join(self.tmp.name, "work", "there"))
        os.makedirs(self.here)
        os.makedirs(self.there)
        for path, text in ((self.here, "ask from here"), (self.there, "ask from there")):
            helpers.write_jsonl(self.slugdir(path, "s.jsonl"), [
                {"type": "user", "message": {"content": text}, "timestamp": "2099-01-01T00:00:00Z"}])
        self.out = os.path.join(self.tmp.name, "c.jsonl")
        self._cwd, self._env = os.getcwd(), dict(os.environ)
        os.chdir(self.here)

    def tearDown(self):
        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def slugdir(self, path, *rest):
        d = os.path.join(self.root, ccpaths.project_slug(path))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, *rest)

    def prompts(self, *extra):
        rc, err = quiet(mine.main, list(extra) + ["--out", self.out, "--days", "0"])
        self.assertEqual(rc, 0, err)
        with open(self.out) as f:
            return sorted(json.loads(x)["prompt"] for x in f)

    def test_explicit_root_scoped_to_current_project(self):
        self.assertEqual(self.prompts(self.root), ["ask from here"])

    def test_project_flag(self):
        self.assertEqual(self.prompts(self.root, "--project", self.there), ["ask from there"])

    def test_all_projects(self):
        self.assertEqual(self.prompts(self.root, "--all-projects"), ["ask from here", "ask from there"])

    def test_explicit_file_and_project_dir_read_as_is(self):
        f = self.slugdir(self.there, "s.jsonl")
        self.assertEqual(self.prompts(f), ["ask from there"])
        self.assertEqual(self.prompts(os.path.dirname(f)), ["ask from there"])

    def test_missing_project_fails_closed(self):
        os.chdir(self.tmp.name)
        rc, err = quiet(mine.main, [self.root, "--out", self.out])
        self.assertEqual(rc, 2)
        self.assertIn("--all-projects", err)
        self.assertFalse(os.path.exists(self.out))

    def test_default_root_honors_claude_config_dir(self):
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp.name  # its projects/ is self.root
        self.assertEqual(self.prompts(), ["ask from here"])
        self.assertEqual(self.prompts("--all-projects"), ["ask from here", "ask from there"])

    def test_project_and_all_projects_are_exclusive(self):
        rc, _ = quiet(mine.main, ["--project", ".", "--all-projects", "--out", self.out])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
