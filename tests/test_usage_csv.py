import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "audit"))
import usage_csv  # noqa: E402

CURRENT = ("skill,model_invocations,typed_invocations,invocations,last_used,description_length,suggested_tier\n"
           "a,2,3,5,2026-10-09,120,keep\n"
           "b,0,0,0,,,name-only\n")
LEGACY = ("skill,override,auto_tool_use,typed_slash,sessions,last_used\n"
          "a,on,2,3,4,2026-10-09\n"
          "b,name-only,0,1,1,2026-09-01\n")
ALIASED = ("skill,model_invocations,typed_invocations,invocations,last_used,description_length,suggested_tier,"
           "auto_tool_use,typed_slash\n"
           "a,2,3,5,2026-10-09,120,keep,2,3\n")


class LoadUsageTests(unittest.TestCase):
    def test_current_schema(self):
        u = usage_csv.load_usage(io.StringIO(CURRENT))
        self.assertEqual((u["a"]["model_invocations"], u["a"]["typed_invocations"], u["a"]["invocations"]), (2, 3, 5))
        self.assertEqual(u["b"]["invocations"], 0)
        self.assertEqual(u["a"]["suggested_tier"], "keep")

    def test_legacy_schema_is_mapped_and_totalled(self):
        u = usage_csv.load_usage(io.StringIO(LEGACY))
        self.assertEqual((u["a"]["model_invocations"], u["a"]["typed_invocations"], u["a"]["invocations"]), (2, 3, 5))
        self.assertEqual(u["b"]["invocations"], 1)
        self.assertEqual(u["b"]["last_used"], "2026-09-01")

    def test_aliased_schema_prefers_current_columns(self):
        u = usage_csv.load_usage(io.StringIO(ALIASED))
        self.assertEqual(u["a"]["invocations"], 5)

    def test_path_argument(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "u.csv")
            with open(p, "w") as f:
                f.write(LEGACY)
            self.assertEqual(usage_csv.load_usage(p)["a"]["invocations"], 5)

    def test_unknown_header_raises_named_error(self):
        with self.assertRaises(usage_csv.UsageSchemaError) as cm:
            usage_csv.load_usage(io.StringIO("skill,count\na,1\n"))
        self.assertIn("count", str(cm.exception))
        self.assertIn("auto_tool_use", str(cm.exception))


class WriterHelperTests(unittest.TestCase):
    def test_header_and_alias_row(self):
        self.assertEqual(usage_csv.header()[-2:], ["auto_tool_use", "typed_slash"])
        self.assertEqual(usage_csv.header(False), usage_csv.HEADER)
        row = ["a", 2, 3, 5, "2026-10-09", 120, "keep"]
        self.assertEqual(usage_csv.alias_row(row), row + [2, 3])


if __name__ == "__main__":
    unittest.main()
