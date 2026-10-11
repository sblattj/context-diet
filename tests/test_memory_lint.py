import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
spec = importlib.util.spec_from_file_location("memory_lint", os.path.join(KIT, "audit", "memory-lint.py"))
ml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ml)

DUP = "Always run the full test suite before you claim that a change is finished."


def put(path, text):
    with open(path, "w") as fh:
        fh.write(text)


class MemoryLintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def w(self, name, text):
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def rules(self, *paths):
        return [f["rule"] for f in ml.lint(list(paths))[0]]

    def test_clean_is_quiet(self):
        self.w("rules/a.md", "short note\n")
        p = self.w("CLAUDE.md", "# T\n@rules/a.md\nSee `rules/a.md` and [x](rules/a.md).\n"
                   "Use the deploy skill. Visit https://x.io/a/b.md or `src/*.py` or `<dir>/a.md`.\n")
        self.assertEqual(self.rules(p), [])

    def test_ml001_missing_import(self):
        p = self.w("CLAUDE.md", "@nope.md\n")
        fs, _ = ml.lint([p])
        self.assertEqual([(f["rule"], f["severity"], f["line"]) for f in fs], [("ML001", "error", 1)])

    def test_ml001_import_relative_to_importer(self):
        self.w("sub/b.md", "ok\n")
        self.w("sub/a.md", "@b.md\n")
        p = self.w("CLAUDE.md", "@sub/a.md\n")
        self.assertEqual(self.rules(p), [])

    def test_ml002_dangling_path(self):
        p = self.w("CLAUDE.md", "See `docs/missing.md` and [g](docs/gone.md#x) and `$HOME/a/b.md` "
                   "and `/abs/none.txt`.\n")
        fs, _ = ml.lint([p])
        self.assertEqual([f["rule"] for f in fs], ["ML002"] * 3)
        self.assertTrue(all(f["severity"] == "warning" for f in fs))

    def test_ml002_repo_root_relative_and_slash_commands(self):
        # a nested instruction file citing a path from its repository root, and slash commands
        self.w("repo/zsh/plugins/exports.zsh", "x\n")
        p = self.w("repo/private/claude/AGENTS.md",
                   "Set in `zsh/plugins/exports.zsh`. Run `/deploy` or `/sync-all`; see `zsh/none.zsh`.\n")
        fs, _ = ml.lint([p])
        self.assertEqual([(f["rule"], f["message"].rsplit(": ", 1)[-1]) for f in fs],
                         [("ML002", "zsh/none.zsh")])

    def test_ml002_ignores_code_fence(self):
        p = self.w("CLAUDE.md", "```\n`docs/missing.md`\n@nope.md\n```\n")
        self.assertEqual(self.rules(p), [])

    def test_ml003_cycle_terminates(self):
        a = self.w("a.md", "@b.md\n")
        self.w("b.md", "@a.md\n")
        fs, files = ml.lint([a])
        self.assertEqual([f["rule"] for f in fs], ["ML003"])
        self.assertEqual(len(files), 2)

    def test_ml003_self_import(self):
        a = self.w("a.md", "@a.md\n")
        self.assertEqual(self.rules(a), ["ML003"])

    def test_ml004_duplicate_within_and_across_import(self):
        self.w("shared.md", "- **%s**\n" % DUP)
        p = self.w("CLAUDE.md", "@shared.md\n1. %s\n# %s\nshort dup line\nshort dup line\n" % (DUP.upper(), DUP))
        fs, _ = ml.lint([p])
        self.assertEqual([f["rule"] for f in fs], ["ML004"])
        self.assertEqual(len(fs[0]["locations"]), 2)

    def test_ml004_import_twice_is_not_duplicate(self):
        self.w("shared.md", DUP + "\n")
        self.w("x.md", "@shared.md\n")
        p = self.w("CLAUDE.md", "@shared.md\n@x.md\n")
        self.assertEqual(self.rules(p), [])

    def test_ml004_short_lines_ignored(self):
        p = self.w("CLAUDE.md", "Be brief and clear.\nBe brief and clear.\n")
        self.assertEqual(self.rules(p), [])

    def test_ml005_long_file_without_pointers(self):
        p = self.w("CLAUDE.md", "".join("step %d does a unique thing\n" % i for i in range(61)))
        fs, _ = ml.lint([p])
        self.assertEqual([(f["rule"], f["severity"]) for f in fs], [("ML005", "info")])

    def test_ml005_quiet_with_pointer_or_short(self):
        body = "".join("step %d does a unique thing\n" % i for i in range(61))
        self.assertEqual(self.rules(self.w("a.md", body + "Use the release skill.\n")), [])
        self.assertEqual(self.rules(self.w("b.md", "step\n" * 10)), [])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memlint-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, *args, env=None):
        e = dict(os.environ, **(env or {}))
        return subprocess.run([sys.executable, os.path.join(KIT, "audit", "memory-lint.py"), *args],
                              capture_output=True, text=True, env=e)

    def test_exit_codes_and_json(self):
        bad = os.path.join(self.tmp, "bad.md")
        good = os.path.join(self.tmp, "good.md")
        put(bad, "@nope.md\n")
        put(good, "fine\n")
        r = self.run_cli("--json", bad)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["findings"][0]["rule"], "ML001")
        self.assertEqual(self.run_cli(good).returncode, 0)
        self.assertEqual(self.run_cli(os.path.join(self.tmp, "none.md")).returncode, 2)

    def test_warning_only_exits_zero(self):
        p = os.path.join(self.tmp, "w.md")
        put(p, "see `docs/missing.md`\n")
        self.assertEqual(self.run_cli(p).returncode, 0)

    def test_default_global_and_project(self):
        cfg = os.path.join(self.tmp, "cfg")
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(cfg)
        os.makedirs(os.path.join(proj, ".claude"))
        put(os.path.join(cfg, "CLAUDE.md"), "global\n")
        put(os.path.join(proj, ".claude", "CLAUDE.md"), "@missing.md\n")
        env = {"CLAUDE_CONFIG_DIR": cfg}
        self.assertEqual(self.run_cli(env=env).returncode, 0)
        r = self.run_cli("--project", proj, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("ML001", r.stdout)


if __name__ == "__main__":
    unittest.main()
