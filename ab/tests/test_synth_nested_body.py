import json
import os
import shutil
import tempfile
import unittest

import helpers  # noqa: F401  (sets sys.path)
import _claude
import make_arms
import synth

M = "fake-model"
DESC = "Use when shipping a release candidate through the staging gate UNIQUEDESCTOKEN"
WHEN = "WHENTOKEN9 only when asked about staging"
BODY = "Step one: tag the build.\nStep two: promote to staging.\n"


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def fm(desc, body, extra=""):
    return "---\ndescription: %s\n%s---\n%s" % (desc, extra, body)


class NestedDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        write(self.d, "team/dev/context.md", fm("ctx", "body"))
        write(self.d, "team/dev/review.md", fm("rev", "body"))
        write(self.d, "team/ops.md", fm("ops", "body"))
        write(self.d, "top.md", fm("top", "body"))
        write(self.d, "README.md", "# readme")
        write(self.d, "team/README.md", "# readme")
        write(self.d, "deploy-helper/SKILL.md", fm("dh", "body"))

    def test_make_arms_names(self):
        self.assertEqual(sorted(make_arms.discover(self.d)),
                         ["deploy-helper", "team:dev:context", "team:dev:review", "team:ops", "top"])

    def test_synth_read_skill_names(self):
        info, _ = synth.read_skill(self.d)
        self.assertEqual(sorted(info), ["deploy-helper", "team:dev:context", "team:dev:review", "team:ops", "top"])
        self.assertEqual(info["team:dev:context"], "ctx")


class DiscoveryFixesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_hidden_dirs_skipped(self):
        write(self.d, ".git/x/notes.md", "n")
        write(self.d, ".hidden.md", "n")
        write(self.d, "ok.md", fm("d", "b"))
        self.assertEqual(make_arms.discover(self.d), ["ok"])

    def test_skill_dir_under_namespace_named_by_dir(self):
        write(self.d, "ns/foo/SKILL.md", fm("d", "b"))
        write(self.d, "ns/cmd.md", fm("d", "b"))
        self.assertEqual(sorted(make_arms.discover(self.d)), ["foo", "ns:cmd"])


class ParseReplyFixesTest(unittest.TestCase):
    def test_non_list_rejected(self):
        with self.assertRaises(ValueError):
            synth.parse_body_reply('{"positives": "abc", "negatives": []}', 2, 1)
        with self.assertRaises(ValueError):
            synth.parse_body_reply('{"positives": ["a"], "negatives": "abc"}', 2, 1)

    def test_plain_array_with_negatives_requested_raises(self):
        with self.assertRaises(ValueError):
            synth.parse_body_reply('["a", "b"]', 2, 1)

    def test_plain_array_without_negatives_ok(self):
        self.assertEqual(synth.parse_body_reply('["a", "b"]', 2, 0), (["a", "b"], []))

    def test_negative_count_rejected(self):
        with self.assertRaises(SystemExit):
            synth.main(["c.jsonl", "--skills-dir", "x", "--model", M, "--no-selftest", "--negatives", "-1"])


class BodyModeTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.sk = os.path.join(self.d, "skills")
        write(self.sk, "team/dev/context.md",
              fm(DESC, BODY, "when_to_use: %s\n" % WHEN))
        self.cases = os.path.join(self.d, "cases.jsonl")
        open(self.cases, "w").close()
        self.prompts = []
        orig = _claude.run_claude
        self.addCleanup(setattr, _claude, "run_claude", orig)

        def fake(args, **kw):
            p = args[args.index("-p") + 1]
            self.prompts.append(p)
            if "near-miss" in p:
                res = {"positives": ["roll the build out to the test cluster"],
                       "negatives": ["what is a good name for my cat"]}
            else:
                res = ["roll the build out to the test cluster"]
            return json.dumps({"type": "result", "is_error": False, "result": json.dumps(res)}), ""
        _claude.run_claude = fake

    def rows(self):
        return _claude.read_jsonl(self.cases)

    def test_body_prompt_excludes_description(self):
        rc = synth.main([self.cases, "--skills-dir", self.sk, "--model", M, "--no-selftest",
                         "--source", "body", "--per-skill", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.prompts), 1)
        p = self.prompts[0]
        self.assertIn("promote to staging", p)
        self.assertNotIn("description:", p)
        for tok in ("UNIQUEDESCTOKEN", "WHENTOKEN9", "when_to_use", DESC):
            self.assertNotIn(tok, p)
        r = self.rows()
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["source"], "synth-body")
        self.assertEqual(r[0]["skills"], ["team:dev:context"])
        self.assertTrue(r[0]["synthetic"])

    def test_default_source_is_description(self):
        synth.main([self.cases, "--skills-dir", self.sk, "--model", M, "--no-selftest", "--per-skill", "1"])
        self.assertIn("UNIQUEDESCTOKEN", self.prompts[0])
        self.assertNotIn("source", self.rows()[0])

    def test_negatives(self):
        synth.main([self.cases, "--skills-dir", self.sk, "--model", M, "--no-selftest",
                    "--source", "body", "--per-skill", "1", "--negatives", "1"])
        self.assertIn("near-miss", self.prompts[0])
        self.assertNotIn("UNIQUEDESCTOKEN", self.prompts[0])
        by = {r["label"]: r for r in self.rows()}
        self.assertEqual(by["negative"]["skills"], [])
        self.assertEqual(by["negative"]["source"], "synth-body")
        self.assertEqual(by["positive"]["skills"], ["team:dev:context"])

    def test_body_cap(self):
        write(self.sk, "big/SKILL.md", fm("d", "x" * 50000))
        synth.main([self.cases, "--skills-dir", self.sk, "--model", M, "--no-selftest", "--source", "body"])
        self.assertTrue(all(len(p) < synth.BODY_CHAR_LIMIT + 2000 for p in self.prompts))

    def test_read_skill_body_strips_frontmatter(self):
        full = synth.read_skill_full(self.sk)
        self.assertEqual(full["team:dev:context"]["body"].strip(), BODY.strip())


if __name__ == "__main__":
    unittest.main()
