"""CLAUDE_CONFIG_DIR, per-project scoping, and start_tokens session filtering."""
import contextlib
import csv
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
KIT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(KIT, "lib"))
sys.path.insert(0, os.path.join(KIT, "audit"))
sys.path.insert(0, HERE)

import ccpaths  # noqa: E402
import fixtures_helper as fx  # noqa: E402


def load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(KIT, *parts))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


usage = load("skill_usage_scope", "audit", "skill-usage.py")
audit = load("audit_scope", "audit", "audit.py")
st = load("start_tokens", "measure", "start_tokens.py")

NOW = "2026-01-31T00:00:00+00:00"


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ctxdiet-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._cwd, self._env = os.getcwd(), dict(os.environ)
        self.addCleanup(self.restore)
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "cfg")
        self.here = os.path.join(self.tmp, "work", "my.repo")
        self.there = os.path.join(self.tmp, "work", "other_repo")
        os.makedirs(self.here)
        os.makedirs(self.there)
        os.chdir(self.here)

    def restore(self):
        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)

    def sessdir(self, project):
        return os.path.join(self.tmp, "cfg", "projects", ccpaths.project_slug(project))

    def run_main(self, fn, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = fn(argv)
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()


class CcpathsTests(EnvCase):
    def test_config_dir_env_and_default(self):
        self.assertEqual(ccpaths.config_dir(), os.path.join(self.tmp, "cfg"))
        self.assertEqual(ccpaths.projects_dir(), os.path.join(self.tmp, "cfg", "projects"))
        del os.environ["CLAUDE_CONFIG_DIR"]
        self.assertEqual(ccpaths.config_dir(), os.path.join(os.path.expanduser("~"), ".claude"))
        os.environ["CLAUDE_CONFIG_DIR"] = ""
        self.assertEqual(ccpaths.config_dir(), os.path.join(os.path.expanduser("~"), ".claude"))

    def test_slug_rule(self):
        # Matches Claude Code's dir names: every non-alphanumeric char becomes '-'.
        self.assertEqual(ccpaths.project_slug("/a/b.c"), "-a-b-c")
        self.assertEqual(ccpaths.project_slug("/x/my_repo-2/.wt"), "-x-my-repo-2--wt")
        link = os.path.join(self.tmp, "link")
        os.symlink(self.here, link)
        self.assertEqual(ccpaths.project_slug(link), ccpaths.project_slug(self.here))  # symlinks resolved

    def test_scoped_dir_fails_closed(self):
        with self.assertRaises(ccpaths.ScopeError):
            ccpaths.scoped_dir()
        os.makedirs(self.sessdir(self.here))
        self.assertEqual(ccpaths.scoped_dir(), self.sessdir(self.here))
        self.assertEqual(ccpaths.scoped_dir(all_projects=True), ccpaths.projects_dir())


class SkillUsageScopeTests(EnvCase):
    def setUp(self):
        super().setUp()
        fx.write_jsonl(os.path.join(self.sessdir(self.here), "s.jsonl"), [fx.skill_call("alpha", "2026-01-30T10:00:00Z")])
        fx.write_jsonl(os.path.join(self.sessdir(self.there), "s.jsonl"), [fx.skill_call("beta", "2026-01-30T10:00:00Z")])
        fx.write_skill(os.path.join(self.tmp, "cfg", "skills"), "alpha", "Short.")

    def counts(self, *extra):
        rc, out, err = self.run_main(usage.main, list(extra) + ["--now", NOW])
        self.assertEqual(rc, 0, err)
        return {r["skill"]: int(r["invocations"]) for r in csv.DictReader(io.StringIO(out))}

    def test_default_is_current_project_and_config_skills(self):
        self.assertEqual(self.counts(), {"alpha": 1})  # skills dir also comes from CLAUDE_CONFIG_DIR

    def test_project_and_all_projects(self):
        self.assertEqual(self.counts("--project", self.there), {"alpha": 0, "beta": 1})
        self.assertEqual(self.counts("--all-projects"), {"alpha": 1, "beta": 1})

    def test_explicit_root_is_scoped_and_project_dir_read_as_is(self):
        root = os.path.join(self.tmp, "cfg", "projects")
        self.assertEqual(self.counts(root), {"alpha": 1})
        self.assertEqual(self.counts(self.sessdir(self.there)), {"alpha": 0, "beta": 1})

    def test_missing_project_exits_2(self):
        os.chdir(self.tmp)
        rc, out, err = self.run_main(usage.main, ["--now", NOW])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("no Claude Code sessions for project", err)


class AuditConfigDirTests(EnvCase):
    def test_settings_and_skills_default_to_config_dir(self):
        cfg = os.path.join(self.tmp, "cfg")
        fx.write_skill(os.path.join(cfg, "skills"), "rare", "Short.")
        with open(os.path.join(cfg, "settings.json"), "w") as f:
            json.dump({"skillOverrides": {"rare": "user-invocable-only"}}, f)
        csvp = os.path.join(self.tmp, "u.csv")
        with open(csvp, "w") as f:
            f.write("skill,model_invocations,typed_invocations,last_used\nrare,9,0,2026-01-30\n")
        rc, out, err = self.run_main(audit.main, [csvp])
        self.assertEqual(rc, 0, err)
        # Row exists only if both the skill (cfg/skills) and its state (cfg/settings.json) were read.
        self.assertIn("| user-invocable-only | on | 9 |", out)


def user(text, entrypoint="cli", **kw):
    d = {"type": "user", "entrypoint": entrypoint, "timestamp": "2026-01-30T10:00:00Z",
         "message": {"role": "user", "content": text}}
    d.update(kw)
    return d


def asst(start):
    return {"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": start}}}


class StartTokensTests(EnvCase):
    def write(self, project, name, events):
        p = os.path.join(self.sessdir(project), name + ".jsonl")
        fx.write_jsonl(p, events)
        return p

    def setUp(self):
        super().setUp()
        self.write(self.here, "fresh1", [user("hi"), asst(1000)])
        self.write(self.here, "fresh2", [user("hello"), asst(3000)])
        self.write(self.here, "fresh3", [user("hey"), asst(2000)])
        self.resumed = self.write(self.here, "resumed", [
            user("This session is being continued from a previous conversation that ran out of context."), asst(90000)])
        self.write(self.here, "compact", [user("summary", isCompactSummary=True), asst(80000)])
        self.write(self.here, "headless", [user("Reply with exactly: OK", entrypoint="sdk-cli"), asst(500)])
        self.write(self.there, "elsewhere", [user("other"), asst(7777)])

    def starts(self, *argv):
        rc, out, err = self.run_main(st.main, list(argv) + ["--json"])
        self.assertEqual(rc, 0, err)
        return sorted(r["start"] for r in json.loads(out))

    def test_resumed_and_non_cli_excluded(self):
        self.assertEqual(self.starts(), [1000, 2000, 3000])
        self.assertEqual(self.starts("--all-entrypoints"), [500, 1000, 2000, 3000])  # resumed still out

    def test_median_is_over_fresh_cli_sessions_only(self):
        rc, out, err = self.run_main(st.main, [])
        self.assertEqual(rc, 0, err)
        self.assertIn("Median start: 2,000 over 3 session(s).", out)

    def test_explicit_file_shown_as_is(self):
        rc, out, _ = self.run_main(st.main, [self.resumed, "--json"])
        rows = json.loads(out)
        self.assertEqual([r["start"] for r in rows], [90000])
        self.assertTrue(rows[0]["resumed"])

    def test_scope_flags(self):
        self.assertEqual(self.starts("--project", self.there), [7777])
        self.assertEqual(self.starts("--all-projects", "--last", "10"), [1000, 2000, 3000, 7777])
        self.assertEqual(self.starts("--dir", self.sessdir(self.there)), [7777])

    def test_missing_project_exits_2(self):
        os.chdir(self.tmp)
        rc, out, err = self.run_main(st.main, [])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn("--all-projects", err)


if __name__ == "__main__":
    unittest.main()
