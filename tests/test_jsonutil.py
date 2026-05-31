import unittest

from codex_cognitive_runtime.jsonutil import extract_json_object


class JsonUtilTest(unittest.TestCase):
    def test_extract_plain_json(self):
        self.assertEqual(extract_json_object('{"ok": true}'), {"ok": True})

    def test_extract_fenced_json(self):
        self.assertEqual(extract_json_object('```json\n{"ok": true}\n```'), {"ok": True})
