import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

KIT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPT = os.path.join(KIT, "audit", "handoff-lint.py")
spec = importlib.util.spec_from_file_location("handoff_lint", SCRIPT)
hl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hl)

HIDDEN = ["ops:deploy", "ops:rollback"]


class HandoffLintTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="handoff-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def w(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def lint(self, hidden=HIDDEN, **kw):
        return hl.lint(self.root, hidden, **kw)[0]

    def where(self, findings):
        return ["%s:%d" % (f["file"], f["line"]) for f in findings]

    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT, "--root", self.root, *args],
                              capture_output=True, text=True)

    def test_flags_namespaced_ref_with_file_and_line(self):
        self.w(".claude/commands/ship.md", "# s\n\nNext, run the gate via /ops:deploy now.\n")
        self.assertEqual(self.where(self.lint()), [".claude/commands/ship.md:3"])

    def test_flags_agents_claude_md_and_skills(self):
        self.w(".claude/agents/a.md", "hand off to /ops:rollback\n")
        self.w("CLAUDE.md", "use /ops:deploy\n")
        self.w(".claude/skills/s/SKILL.md", "---\nname: s\n---\ninvoke /ops:deploy\n")
        self.assertEqual(sorted(self.where(self.lint())),
                         [".claude/agents/a.md:1", ".claude/skills/s/SKILL.md:4", "CLAUDE.md:1"])

    def test_three_part_ref(self):
        self.w("CLAUDE.md", "then /ops:prod:push\n")
        self.assertEqual(len(self.lint(["ops:prod:push"])), 1)

    def test_allows_user_type_phrasing(self):
        lines = ["Ask the user to type /ops:deploy when ready.",
                 "The user can type `/ops:deploy` themselves.",
                 "Tell the user to run /ops:rollback.",
                 "The developer should run /ops:deploy."]
        self.w(".claude/commands/a.md", "\n".join(lines) + "\n")
        self.assertEqual(self.lint(), [])

    def test_model_handoff_is_flagged_even_with_run(self):
        self.w(".claude/commands/a.md", "Then invoke /ops:deploy and run /ops:rollback.\n")
        self.assertEqual(len(self.lint()), 2)

    def test_bare_you_never_allows_and_judged_per_ref(self):
        self.w(".claude/agents/a.md", "\n".join([
            "You must run /ops:deploy before merging",
            "Before you run tests, invoke /ops:deploy",
            "recommending the developer run /ops:deploy",
            "the user should type /ops:deploy, then you invoke /ops:rollback",
            "ask the user to type /ops:deploy",
            ""]))
        self.assertEqual([(f["line"], f["command"]) for f in self.lint()],
                         [(1, "ops:deploy"), (2, "ops:deploy"), (4, "ops:rollback")])

    def test_ignores_visible_commands_paths_and_empty_hidden_set(self):
        self.w(".claude/commands/a.md", "See /ops:status and docs/ops:deploy and /usr/bin/env.\n")
        self.assertEqual(self.lint(), [])
        self.assertEqual(self.lint([]), [])

    def test_front_matter_is_not_body(self):
        self.w(".claude/commands/a.md", "---\ndescription: \"/ops:deploy\"\n---\nbody\n")
        self.assertEqual(self.lint(), [])

    def test_bare_name_needs_flag(self):
        self.w("CLAUDE.md", "see /review and /ops:deploy\n")
        self.assertEqual(self.lint(["review"]), [])
        self.assertEqual([f["ref"] for f in self.lint(["review"], bare=True)], ["/review"])

    def test_namespace_prefix_is_dropped(self):
        self.w("CLAUDE.md", "go /acme:ops:deploy and /acme:review\n")
        f = self.lint(["ops:deploy", "review"], namespaces=("acme",))
        self.assertEqual([x["command"] for x in f], ["ops:deploy", "review"])

    def test_configurable_prefix_and_separator(self):
        self.w("CLAUDE.md", "run $ops.deploy now and /ops:deploy\n")
        f = self.lint(prefix="$", separator=".")
        self.assertEqual([x["ref"] for x in f], ["$ops.deploy"])

    def test_hidden_from_front_matter(self):
        self.w(".claude/commands/ops/deploy.md", "---\ndisable-model-invocation: true\n---\nx\n")
        self.w(".claude/commands/ops/status.md", "---\ndescription: d\n---\nx\n")
        self.w(".claude/commands/help.md", "---\ndisable-model-invocation: true\n---\nx\n")
        self.w(".claude/skills/audit-log/SKILL.md", "---\nname: audit-log\ndisable-model-invocation: true\n---\nx\n")
        self.assertEqual(hl.hidden_from_front_matter(self.root), ["audit-log", "help", "ops:deploy"])

    def test_hidden_from_settings_overrides(self):
        p = self.w("settings.json", json.dumps({"skillOverrides": {
            "ops:deploy": "user-invocable-only", "acme:review": "off", "ops:status": "on", "x": "name-only"}}))
        self.assertEqual(hl.hidden_from_settings(p, ("acme",)), ["ops:deploy", "review"])

    def test_cli_exit_codes_text_and_hidden_list(self):
        self.w(".claude/commands/a.md", "go /ops:deploy\n")
        lst = self.w("hidden.json", json.dumps(["ops/deploy"]))
        bad = self.run_cli("--hidden", lst)
        self.assertEqual(bad.returncode, 1)
        self.assertIn(".claude/commands/a.md:1  /ops:deploy", bad.stdout)
        ok = self.run_cli()
        self.assertEqual(ok.returncode, 0)
        self.assertIn("0 finding(s)", ok.stdout)

    def test_cli_accepts_commands_object_list(self):
        self.w("CLAUDE.md", "see /ops:rollback\n")
        lst = self.w("h.json", json.dumps({"commands": ["ops/rollback"]}))
        self.assertEqual(self.run_cli("--hidden", lst).returncode, 1)

    def test_cli_json(self):
        self.w(".claude/commands/ops/deploy.md", "---\ndisable-model-invocation: true\n---\nx\n")
        self.w(".claude/agents/a.md", "call /ops:deploy\n")
        r = self.run_cli("--json")
        self.assertEqual(r.returncode, 1)
        d = json.loads(r.stdout)
        self.assertEqual(d["hidden"], ["ops:deploy"])
        self.assertEqual(d["findings"], [{"file": ".claude/agents/a.md", "line": 1,
                                          "ref": "/ops:deploy", "command": "ops:deploy"}])

    def test_cli_settings_and_scan(self):
        s = self.w("s.json", json.dumps({"skillOverrides": {"ops:deploy": "off"}}))
        self.w("notes/n.md", "call /ops:deploy\n")
        self.assertEqual(self.run_cli("--settings", s).returncode, 0)
        self.assertEqual(self.run_cli("--settings", s, "--scan", "notes").returncode, 1)

    def test_cli_bad_hidden_exits_2(self):
        for body in ['{"a":1}', "[1]", "nope"]:
            lst = self.w("h.json", body)
            r = self.run_cli("--hidden", lst)
            self.assertEqual(r.returncode, 2, body)
            self.assertIn("--hidden", r.stderr)


if __name__ == "__main__":
    unittest.main()
