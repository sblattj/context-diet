import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
spec = importlib.util.spec_from_file_location("kiro_footprint", os.path.join(ROOT, "measure", "kiro_footprint.py"))
kf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kf)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class StaticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = os.path.join(self.tmp, "ws")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        write(os.path.join(self.ws, ".kiro/steering/style.md"), "a" * 400)
        write(os.path.join(self.ws, ".kiro/steering/team.md"), "b" * 80)
        write(os.path.join(self.ws, "docs/extra.md"), "c" * 200)
        agent = {"name": "dev", "resources": [
            "file://.kiro/steering/style.md", "file://docs/*.md", "skill://x/SKILL.md", {"uri": "file://missing.md"}]}
        write(os.path.join(self.ws, ".kiro/agents/dev.json"), json.dumps(agent))

    def test_counts_and_duplicate_detection(self):
        rep = kf.static_footprint(self.ws, self.home)
        self.assertEqual(rep["workspace_steering_tokens"], 100 + 20)
        a = rep["agents"][0]
        self.assertEqual(a["agent"], "dev")
        self.assertEqual(len(a["duplicates"]), 1)
        self.assertTrue(a["duplicates"][0].endswith("style.md"))
        self.assertEqual(a["duplicate_tokens"], 100)
        self.assertEqual(a["resource_tokens"], 100 + 50)
        self.assertEqual(a["total_tokens"], 120 + 150)  # style.md really counted twice
        self.assertIn("DUPLICATE", kf.render(rep))

    def test_symlink_duplicate_detected(self):
        os.symlink(os.path.join(self.ws, ".kiro/steering/team.md"), os.path.join(self.ws, "docs/link.md"))
        rep = kf.static_footprint(self.ws, self.home)
        self.assertIn(os.path.join(self.ws, "docs/link.md"), rep["agents"][0]["duplicates"])

    def test_personal_steering_separate_and_isolated_home(self):
        write(os.path.join(self.home, ".kiro/steering/mine.md"), "p" * 4000)
        rep = kf.static_footprint(self.ws, self.home)
        self.assertEqual(rep["personal_steering_tokens"], 1000)
        self.assertEqual(rep["workspace_steering_tokens"], 120)
        self.assertTrue(rep["home_isolated"])
        self.assertNotIn("WARNING: real HOME", kf.render(rep))
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertEqual(kf.static_footprint(self.ws, empty)["personal_steering_tokens"], 0)

    def test_real_home_is_flagged(self):
        write(os.path.join(self.home, ".kiro/steering/mine.md"), "p" * 40)
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old else os.environ.pop("HOME", None))
        rep = kf.static_footprint(self.ws)
        self.assertFalse(rep["home_isolated"])
        self.assertEqual(rep["personal_steering_tokens"], 10)
        self.assertIn("WARNING: real HOME", kf.render(rep))

    def test_empty_workspace(self):
        rep = kf.static_footprint(self.tmp, self.home)
        self.assertEqual(rep["workspace_steering_tokens"], 0)
        self.assertEqual(rep["agents"], [])


FAKE = r'''#!/bin/sh
echo "$HOME" > "$FAKE_OUT/home"
echo "$@" > "$FAKE_OUT/args"
if [ -n "$FAKE_SLEEP" ]; then sleep "$FAKE_SLEEP"; fi
cat <<'JSON'
{"data":{"update":{"_meta":{"kiro":{"breakdown":{"contextFiles":{"tokens":321,"items":[{"uri":"file:///w/.kiro/steering/style.md"}]}}}}}}}
{"data":{"update":{"sessionUpdate":"tool_call","toolCallId":"1","rawInput":{"path":"settings.yaml"}}}}
{"type":"runFinished","data":{"finalText":"The color is teal."}}
JSON
'''


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)
        write(os.path.join(self.bin, "kiro-cli"), FAKE)
        os.chmod(os.path.join(self.bin, "kiro-cli"), 0o755)
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        self._env = dict(os.environ)
        os.environ["PATH"] = self.bin + os.pathsep + os.environ["PATH"]
        os.environ["FAKE_OUT"] = self.out
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_probe_parses_and_isolates_home(self):
        r = kf.live_probe(self.ws, "what color?", agent="dev", expect="teal", read_pattern=r"settings\.yaml",
                          real_home=self.out)
        self.assertEqual(r["context_tokens"], 321)
        self.assertTrue(r["answer_ok"])
        self.assertEqual(r["file_reads"], 1)
        with open(os.path.join(self.out, "home")) as fh:
            used = fh.read().strip()
        self.assertNotEqual(used, self._env.get("HOME"))
        self.assertFalse(os.path.exists(used))  # temp HOME cleaned up
        with open(os.path.join(self.out, "args")) as fh:
            args = fh.read()
        self.assertIn("--v3", args)
        self.assertIn("--agent dev", args)

    def test_wrong_answer(self):
        r = kf.live_probe(self.ws, "q", expect="purple", real_home=self.out)
        self.assertFalse(r["answer_ok"])

    def test_timeout(self):
        os.environ["FAKE_SLEEP"] = "5"
        r = kf.live_probe(self.ws, "q", timeout=1, real_home=self.out)
        self.assertIn("timeout", r["error"])

    def test_seed_home_links_login_not_steering(self):
        real = os.path.join(self.tmp, "realhome")
        auth = os.path.join(real, "Library", "Application Support", "kiro-cli")
        os.makedirs(auth)
        write(os.path.join(auth, "data.sqlite3"), "x")
        write(os.path.join(real, ".local", "bin", "kiro-cli-chat"), "#!/bin/sh\n")
        write(os.path.join(real, ".local", "bin", "other-tool"), "#!/bin/sh\n")
        write(os.path.join(real, ".kiro", "steering", "personal.md"), "personal notes")
        iso = os.path.join(self.tmp, "iso")
        os.makedirs(iso)
        linked = kf.seed_home(iso, real)
        self.assertTrue(os.path.exists(os.path.join(iso, "Library", "Application Support", "kiro-cli", "data.sqlite3")))
        self.assertTrue(os.path.islink(os.path.join(iso, ".local", "bin", "kiro-cli-chat")))
        self.assertFalse(os.path.lexists(os.path.join(iso, ".local", "bin", "other-tool")))
        self.assertFalse(os.path.lexists(os.path.join(iso, ".kiro")))
        self.assertEqual(kf.seed_home(iso, real), [])  # idempotent
        self.assertEqual(len(linked), 2)

    def test_bare_home_timeout_hints_at_login(self):
        os.environ["FAKE_SLEEP"] = "5"
        r = kf.live_probe(self.ws, "q", timeout=1, link_auth=False)
        self.assertIn("no kiro login", r["error"])

    def test_skipped_without_kiro(self):
        os.environ["PATH"] = self.out  # no kiro-cli here
        r = kf.live_probe(self.ws, "q")
        self.assertIn("skipped", r)
        self.assertEqual(kf.main([self.ws, "--live", "--prompt", "q"]), 0)

    def test_cli_static(self):
        self.assertEqual(kf.main([self.ws, "--home", self.out, "--json"]), 0)


if __name__ == "__main__":
    unittest.main()
