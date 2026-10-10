"""Scripts must import lib/ and sibling modules when run through a symlink (skill installs)."""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCRIPTS = ["measure/start_tokens.py", "ab/mine.py", "audit/audit.py",
           "audit/skill-usage.py", "audit/skill-lint.py"]


class SymlinkImport(unittest.TestCase):
    def test_help_through_file_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            for rel in SCRIPTS:
                link = os.path.join(d, os.path.basename(rel))
                os.symlink(os.path.join(ROOT, rel), link)
                r = subprocess.run([sys.executable, link, "-h"], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr[-300:]}")


if __name__ == "__main__":
    unittest.main()
