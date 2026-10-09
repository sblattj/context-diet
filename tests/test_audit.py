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
