import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import fixtures_helper as fx  # noqa: E402

spec = importlib.util.spec_from_file_location("size_ratchet", os.path.join(ROOT, "audit", "size-ratchet.py"))
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)


class RatchetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = os.path.join(self.tmp, "cfg")
        os.makedirs(os.path.join(self.cfg, "agents"))
        self.base = os.path.join(self.tmp, "base.json")
        self._old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old

    def put(self, rel, text):
        p = os.path.join(self.cfg, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = sr.main(["--baseline", self.base] + list(args))
        return rc, out.getvalue()

    def test_pin_then_clean(self):
        self.put("CLAUDE.md", "x" * 100)
        self.put("agents/a.md", "y" * 40)
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("pinned 2 entries", out)
        self.assertEqual(json.load(open(self.base)), {"agent a.md": 40, "claude-md config:CLAUDE.md": 100})
        rc, out = self.run_main()
        self.assertEqual(rc, 0)

    def test_growth_fails_with_delta(self):
        self.put("CLAUDE.md", "x" * 100)
        self.run_main()
        self.put("CLAUDE.md", "x" * 130)
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("GREW claude-md config:CLAUDE.md +30 bytes (100 -> 130)", out)

    def test_shrink_gone_new(self):
        self.put("CLAUDE.md", "x" * 100)
        self.put("agents/a.md", "y")
        self.run_main()
        self.put("CLAUDE.md", "x" * 60)
        os.remove(os.path.join(self.cfg, "agents", "a.md"))
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("SHRANK claude-md config:CLAUDE.md -40", out)
        self.assertIn("GONE agent a.md", out)
        self.put("agents/b.md", "zz")
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("NEW agent b.md 2 bytes", out)

    def test_import_growth_charged_to_importer(self):
        self.put("CLAUDE.md", "intro\n@extra.md\n")
        self.put("extra.md", "e" * 50)
        self.run_main()
        base = json.load(open(self.base))
        self.assertEqual(base["claude-md config:CLAUDE.md"], len("intro\n@extra.md\n") + 50)
        self.assertEqual(base["import config:extra.md"], 50)
        self.put("extra.md", "e" * 80)
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("GREW claude-md config:CLAUDE.md +30 bytes", out)
        self.assertIn("GREW import config:extra.md +30 bytes", out)

    def test_cycle_safe(self):
        self.put("CLAUDE.md", "@a.md\n")
        self.put("a.md", "A\n@b.md\n")
        self.put("b.md", "B\n@a.md\n@CLAUDE.md\n")
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        base = json.load(open(self.base))
        self.assertEqual(base["claude-md config:CLAUDE.md"], 6 + 8 + 19)

    def test_project_files(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(proj, ".claude"))
        open(os.path.join(proj, "CLAUDE.md"), "w").write("p" * 7)
        open(os.path.join(proj, ".claude", "CLAUDE.md"), "w").write("q" * 9)
        self.run_main("--project", proj)
        base = json.load(open(self.base))
        self.assertEqual(base, {"claude-md project:CLAUDE.md": 7, "claude-md project:.claude/CLAUDE.md": 9})

    def test_skill_states(self):
        sk = os.path.join(self.cfg, "skills")
        fx.write_skill(sk, "listed", "d" * 30, when_to_use="w" * 5)
        fx.write_skill(sk, "namedonly", "d" * 99)
        fx.write_skill(sk, "hidden", "d" * 99)
        fx.write_skill(sk, "off", "d" * 99)
        self.put("settings.json", json.dumps({"skillOverrides": {
            "namedonly": "name-only", "hidden": "user-invocable-only", "off": "off"}}))
        self.run_main()
        self.assertEqual(json.load(open(self.base)), {"skill listed": 35, "skill namedonly": len("namedonly")})
        fx.write_skill(sk, "namedonly", "d" * 500)  # description growth is invisible when name-only
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        fx.write_skill(sk, "listed", "d" * 40, when_to_use="w" * 5)
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("GREW skill listed +10 bytes (35 -> 45)", out)

    def test_update_and_json(self):
        self.put("CLAUDE.md", "x" * 10)
        self.run_main()
        self.put("CLAUDE.md", "x" * 20)
        rc, out = self.run_main("--update")
        self.assertEqual(rc, 0)
        self.assertEqual(json.load(open(self.base))["claude-md config:CLAUDE.md"], 20)
        self.put("CLAUDE.md", "x" * 25)
        rc, out = self.run_main("--json")
        self.assertEqual(rc, 1)
        j = json.loads(out)
        self.assertFalse(j["ok"])
        self.assertEqual(j["changes"], ["GREW claude-md config:CLAUDE.md +5 bytes (20 -> 25)"])


if __name__ == "__main__":
    unittest.main()
