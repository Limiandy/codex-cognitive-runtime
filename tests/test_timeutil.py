import unittest

from codex_cognitive_runtime.api_schema import ok
from codex_cognitive_runtime.timeutil import local_now_iso, parse_timestamp


class TimeUtilTest(unittest.TestCase):
    def test_local_now_uses_offset_instead_of_z_suffix(self):
        stamp = local_now_iso()
        self.assertNotIn("Z", stamp)
        self.assertRegex(stamp, r"[+-]\d\d:\d\d$")

    def test_api_generated_at_uses_local_offset(self):
        stamp = ok({})["meta"]["generated_at"]
        self.assertNotIn("Z", stamp)
        self.assertRegex(stamp, r"[+-]\d\d:\d\d$")

    def test_parse_timestamp_keeps_legacy_z_compatible(self):
        parsed = parse_timestamp("2026-06-01T00:00:00Z")
        self.assertIsNotNone(parsed.tzinfo)


if __name__ == "__main__":
    unittest.main()
