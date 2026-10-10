import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit"))
import audit  # noqa: E402


class SuggestTests(unittest.TestCase):
    def s(self, model, typed, cur, explicit=True, age=99):
        return audit.suggest(model, typed, cur, explicit, age, rare=3, new_days=30)

    def test_typed_only_default_on_goes_hidden(self):
        self.assertEqual(self.s(0, 2, "on", explicit=False), "user-invocable-only")

    def test_typed_only_explicit_state_is_kept(self):
        self.assertEqual(self.s(0, 2, "name-only"), "name-only")

    def test_frequent_is_listed(self):
        self.assertEqual(self.s(5, 0, "name-only"), "on")

    def test_rare_listed_goes_name_only_or_hidden(self):
        self.assertEqual(self.s(1, 0, "on"), "name-only")
        self.assertEqual(self.s(0, 0, "on"), "user-invocable-only")
        self.assertEqual(self.s(0, 0, "on", age=3), "name-only")

    def test_explicit_hide_kept(self):
        self.assertEqual(self.s(0, 0, "user-invocable-only"), "user-invocable-only")


if __name__ == "__main__":
    unittest.main()


class LegacyCsvTests(unittest.TestCase):
    """audit.py main() reads a schema-1 CSV (auto_tool_use/typed_slash) and a bad one fails clearly."""

    def run_main(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = audit.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_legacy_csv_counts_are_used(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sk = os.path.join(d, "skills", "alpha")
            os.makedirs(sk)
            with open(os.path.join(sk, "SKILL.md"), "w") as f:
                f.write("---\nname: alpha\ndescription: Short.\n---\nBody.\n")
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump({"skillOverrides": {"alpha": "name-only"}}, f)
            csvp = os.path.join(d, "u.csv")
            with open(csvp, "w") as f:
                f.write("skill,override,auto_tool_use,typed_slash,sessions,last_used\nalpha,name-only,5,0,5,2026-10-01\n")
            rc, out, err = self.run_main([csvp, "--settings", os.path.join(d, "settings.json"),
                                          "--skills-dir", os.path.join(d, "skills"), "--new-days", "0"])
            self.assertEqual(rc, 0, err)
            self.assertIn("| alpha | name-only | on | 5 | 0 | 2026-10-01 |", out)

    def test_unknown_csv_exits_2_with_message(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            csvp = os.path.join(d, "u.csv")
            with open(csvp, "w") as f:
                f.write("name,hits\nalpha,1\n")
            rc, out, err = self.run_main([csvp, "--settings", os.path.join(d, "none.json"), "--skills-dir", d])
            self.assertEqual(rc, 2)
            self.assertIn("not a usage CSV", err)
