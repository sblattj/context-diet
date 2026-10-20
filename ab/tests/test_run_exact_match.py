import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _claude  # noqa: E402
import run  # noqa: E402


def stream(*events):
    return "\n".join(json.dumps(e) for e in events)


def tool_use(name, **inp):
    return {"type": "assistant", "message": {"usage": {"input_tokens": 5}, "content": [
        {"type": "tool_use", "name": name, "input": inp}]}}


def result(**kw):
    ev = {"type": "result", "is_error": False, "num_turns": 2, "session_id": "sess-1"}
    ev.update(kw)
    return ev


class ExactMatchTests(unittest.TestCase):
    def test_colliding_leaf_names_do_not_match(self):
        self.assertTrue(run.skill_matches("team:dev:context", "team:dev:context"))
        self.assertTrue(run.skill_matches("Team:Dev:Context", "team:dev:context"))
        self.assertFalse(run.skill_matches("context", "team:context"))
        self.assertFalse(run.skill_matches("team:context", "team:dev:context"))
        self.assertFalse(run.skill_matches("pr-walkthrough", "team:dev:pr-walkthrough"))
        self.assertFalse(run.skill_matches("team:dev:pr-walkthrough", "pr-walkthrough"))

    def test_arm_state_exact(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            json.dump({"skillOverrides": {"team:dev:context": "off", "context": "name-only"}}, open(p, "w"))
            self.assertEqual(run.arm_state(p, "team:dev:context"), "off")
            self.assertEqual(run.arm_state(p, "context"), "name-only")
            self.assertEqual(run.arm_state(p, "team:context"), "on")

    def test_unrename_exact(self):
        meta = {"rename": {"going-all-in": "boil-the-ocean"}}
        self.assertEqual(run.unrename(["going-all-in", "plug:going-all-in"], meta),
                         ["boil-the-ocean", "plug:going-all-in"])

    def test_expected_any_and_none(self):
        c = {"label": "positive", "skills": [], "expected_any": ["a:b", "a:c"]}
        self.assertTrue(run.expectation_met(["a:c"], c))
        self.assertFalse(run.expectation_met(["c"], c))
        neg = {"label": "negative", "skills": ["none"]}
        self.assertTrue(run.expectation_met([], neg))
        self.assertFalse(run.expectation_met(["deploy-helper"], neg))
        self.assertTrue(run.expectation_met(["x"], {"expected_any": ["none", "x"]}))
        self.assertTrue(run.expectation_met([], {"expected_any": ["none", "x"]}))

    def test_excluded_uses_expected_any(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            json.dump({"skillOverrides": {"a:b": "off", "a:c": "off"}}, open(p, "w"))
            self.assertTrue(run.is_excluded({"label": "positive", "expected_any": ["a:b", "a:c"]}, p))
            self.assertFalse(run.is_excluded({"label": "positive", "expected_any": ["a:b", "a:d"]}, p))


class ArmIsolationTests(unittest.TestCase):
    def test_build_args_extra(self):
        extra = ["--setting-sources", "project,local", "--mcp-config", "{}"]
        a = run.build_args("hi", "/x.json", "m", 2, None, extra)
        self.assertEqual(a[-4:], extra)
        self.assertEqual(a.count("--mcp-config"), 1)  # arm's replaces the default
        self.assertEqual(run.build_args("hi", "/x.json", "m").count("--mcp-config"), 1)

    def test_run_one_passes_args_and_env(self):
        with tempfile.TemporaryDirectory() as d:
            arm = os.path.join(d, "a.json")
            json.dump({}, open(arm, "w"))
            json.dump({"claude_args": ["--strict-mcp-config"], "env": {"FOO": "bar"}},
                      open(os.path.join(d, "a.meta.json"), "w"))
            seen = {}

            def fake(args, **kw):
                seen["args"], seen["env"] = args, kw.get("extra_env")
                return stream(tool_use("Skill", skill="x"), result(session_id="none-such")), 0
            opts = mock.Mock(model="m", max_turns=2, tools=None, deadline=5, cwd=d, keep_api_key=False, claude_bin="claude")
            with mock.patch.object(_claude, "run_claude", fake), mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                rec = run.run_one({"id": "c", "prompt": "p", "label": "negative", "skills": []}, "a", arm, 0, opts)
            self.assertEqual(seen["env"], {"FOO": "bar"})
            self.assertEqual(seen["args"][-1], "--strict-mcp-config")
            self.assertIsNone(rec["listing"])  # transcript missing -> null, no crash
            self.assertEqual(rec["skills"], ["x"])


class ListingTests(unittest.TestCase):
    def test_parse_listing(self):
        content = "The following skills are available:\n- deploy-helper: ships it\n- team:dev:context: ctx: more\n- bare-one\n"
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "projects", "slug"))
            with open(os.path.join(d, "projects", "slug", "sess-1.jsonl"), "w") as fh:
                fh.write("not json\n")
                fh.write(json.dumps({"type": "attachment", "attachment": {"type": "skill_listing", "content": content}}) + "\n")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                L = run.read_listing("sess-1")
            self.assertEqual(L["listing_chars"], len(content))
            self.assertEqual(L["listing_entries"], 3)
            self.assertEqual(L["name_only"], ["bare-one"])
            self.assertEqual(L["with_description"], ["deploy-helper", "team:dev:context"])

    def test_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            self.assertIsNone(run.read_listing("nope"))
            self.assertIsNone(run.read_listing(None))


class ReadFireAndErrorTests(unittest.TestCase):
    def test_read_fires(self):
        out = stream(tool_use("Read", file_path="/h/.claude/skills/deploy-helper/SKILL.md"),
                     tool_use("Read", file_path="/h/.claude/commands/team/dev/context.md"),
                     tool_use("Read", file_path="/h/notes.md"),
                     tool_use("Skill", skill="other"), result())
        p = run.parse_stream(out)
        self.assertEqual(p["skills"], ["other"])
        self.assertEqual(p["read_skills"], ["deploy-helper", "team:dev:context"])
        self.assertEqual(p["session_id"], "sess-1")

    def test_fired_via(self):
        with tempfile.TemporaryDirectory() as d:
            arm = os.path.join(d, "a.json")
            json.dump({}, open(arm, "w"))
            out = stream(tool_use("Skill", skill="s1"),
                         tool_use("Read", file_path="/x/skills/s2/SKILL.md"), result())
            opts = mock.Mock(model="m", max_turns=2, tools=None, deadline=5, cwd=d, keep_api_key=False, claude_bin="c")
            with mock.patch.object(_claude, "run_claude", lambda *a, **k: (out, 0)):
                rec = run.run_one({"id": "c", "prompt": "p", "label": "negative", "skills": []}, "a", arm, 0, opts)
            self.assertEqual(rec["skills"], ["s1"])
            self.assertEqual(rec["read_skills"], ["s2"])
            self.assertEqual(rec["fired_via"], {"s1": "skill", "s2": "read"})

    def run_err(self, out=None, exc=None):
        with tempfile.TemporaryDirectory() as d:
            arm = os.path.join(d, "a.json")
            json.dump({}, open(arm, "w"))
            opts = mock.Mock(model="m", max_turns=2, tools=None, deadline=5, cwd=d, keep_api_key=False, claude_bin="c")

            def fake(*a, **k):
                if exc:
                    raise exc
                return out, 0
            with mock.patch.object(_claude, "run_claude", fake):
                return run.run_one({"id": "c", "prompt": "p", "label": "negative", "skills": []}, "a", arm, 0, opts)

    def test_provider_errors_recorded(self):
        rec = self.run_err(stream(result(is_error=True, subtype="success", result="API Error: 429 rate limit exceeded")))
        self.assertTrue(rec["is_error"])
        self.assertEqual(rec["error_class"], "rate_limit")
        self.assertIn("error", rec)
        rec = self.run_err(stream(result(is_error=True, result="API Error: 529 overloaded_error")))
        self.assertEqual(rec["error_class"], "overloaded")
        rec = self.run_err(stream(result(is_error=True, result="Invalid API key, 401")))
        self.assertEqual(rec["error_class"], "auth")
        rec = self.run_err(exc=_claude.ClaudeError("deadline of 5s exceeded"))
        self.assertEqual((rec["is_error"], rec["error_class"]), (True, "timeout"))
        rec = self.run_err("garbage")
        self.assertEqual((rec["is_error"], rec["error_class"]), (True, "no_output"))
        rec = self.run_err(stream(tool_use("Skill", skill="x"), result(is_error=True, result="boom")))
        self.assertEqual(rec["error_class"], "api_error")
        self.assertEqual(rec["skills"], ["x"])

    def test_max_turns_is_not_error(self):
        rec = self.run_err(stream(tool_use("Skill", skill="x"), result(subtype="error_max_turns", is_error=True)))
        self.assertNotIn("error", rec)


class ReviewFixTests(unittest.TestCase):
    def one(self, case, out=None, claude_args=None):
        with tempfile.TemporaryDirectory() as d:
            arm = os.path.join(d, "a.json")
            json.dump({}, open(arm, "w"))
            if claude_args:
                json.dump({"claude_args": claude_args}, open(os.path.join(d, "a.meta.json"), "w"))
            self.seen = []
            opts = mock.Mock(model="m", max_turns=2, tools=None, deadline=5, cwd=d, keep_api_key=False, claude_bin="c")

            def fake(args, **k):
                self.seen = args
                return (out or stream(result())), 0
            with mock.patch.object(_claude, "run_claude", fake):
                return run.run_one(dict({"id": "c", "prompt": "p", "label": "positive"}, **case), "a", arm, 0, opts)

    def test_expected_recorded_from_expected_set(self):
        rec = self.one({"skills": [], "expected_any": ["b:x", "a:y"]})
        self.assertEqual(rec["expected"], ["a:y", "b:x"])
        self.assertFalse(rec.get("expect_none"))
        rec = self.one({"label": "negative", "skills": ["none"]})
        self.assertEqual(rec["expected"], [])
        self.assertTrue(rec["expect_none"])
        rec = self.one({"label": "negative", "expected_any": ["none", "z"]})
        self.assertEqual(rec["expected"], ["z"])
        self.assertTrue(rec["expect_none"])

    def test_read_fire_namespaced_and_anchored(self):
        f = run.read_fire_name
        self.assertEqual(f("/h/.claude/commands/team/dev/context.md"), "team:dev:context")
        self.assertEqual(f(".claude/commands/ctx.md"), "ctx")
        self.assertEqual(f("/h/.claude/skills/team:dev:pr-walkthrough/SKILL.md"), "team:dev:pr-walkthrough")
        self.assertEqual(f("/p/plugins/cache/plug/1.0/commands/a/b.md"), "a:b")
        self.assertEqual(f("/p/plugins/cache/plug/1.0/skills/deploy/SKILL.md"), "deploy")
        self.assertIsNone(f("/repo/docs/commands/x.md"))
        self.assertIsNone(f("/repo/commands/x.md"))
        self.assertIsNone(f("/repo/docs/skills/a/b/SKILL.md"))

    def test_reads_do_not_pollute_skills(self):
        out = stream(tool_use("Skill", skill="s1"),
                     tool_use("Read", file_path="/h/.claude/skills/s2/SKILL.md"), result())
        rec = self.one({"skills": []}, out)
        self.assertEqual(rec["skills"], ["s1"])
        self.assertEqual(rec["read_skills"], ["s2"])
        self.assertEqual(rec["fired_via"], {"s1": "skill", "s2": "read"})

    def test_status_codes_word_boundary(self):
        self.assertEqual(run.classify_error("request id req_14290311 failed"), "api_error")
        self.assertEqual(run.classify_error("id 5290x"), "api_error")
        self.assertEqual(run.classify_error("HTTP 429"), "rate_limit")
        self.assertEqual(run.classify_error("status=401."), "auth")

    def test_mcp_config_equals_form(self):
        self.one({"skills": []}, claude_args=["--mcp-config={}"])
        self.assertEqual(sum(1 for a in self.seen if a.startswith("--mcp-config")), 1)
        a = run.build_args("hi", "/x.json", "m", 2, None, ["--mcp-config={}", "--strict-mcp-config"])
        self.assertEqual(a.count("--strict-mcp-config"), 1)


if __name__ == "__main__":
    unittest.main()
