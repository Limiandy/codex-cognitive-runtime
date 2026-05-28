import json
import unittest
from pathlib import Path


class PluginManifestTest(unittest.TestCase):
    def test_manifest_is_ledger_only(self):
        manifest = json.loads(Path(".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        text = json.dumps(manifest, ensure_ascii=False).lower()
        self.assertNotIn("mempalace", text)
        self.assertIn("ledger", text)
        self.assertIn("sqlite", text)
        self.assertIn("workflow guard", text)


if __name__ == "__main__":
    unittest.main()
