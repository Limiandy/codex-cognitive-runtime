import json
import unittest
from pathlib import Path

from codex_memory import plugin_manager


class PluginConfigTest(unittest.TestCase):
    def test_mcp_config_is_portable(self):
        data = json.loads(Path(".mcp.json").read_text(encoding="utf-8"))
        server = data["mcpServers"]["codex-memory"]
        rendered = json.dumps(server)
        self.assertEqual(server["command"], "bash")
        self.assertIn("CODEX_PLUGIN_ROOT", rendered)
        self.assertNotIn("/Users/" + "limengkai", rendered)
        self.assertNotIn(str(Path.home()), rendered)

    def test_hooks_config_is_portable(self):
        rendered = Path("hooks.json").read_text(encoding="utf-8")
        self.assertIn("CODEX_PLUGIN_ROOT", rendered)
        self.assertNotIn("/Users/" + "limengkai", rendered)
        self.assertNotIn(str(Path.home()), rendered)

    def test_config_update_preserves_unknown_sections_and_validates_toml(self):
        before = """
# keep this comment
[profiles.dev]
model = "gpt-5.4-mini"

[plugins."other@personal"]
enabled = true
"""
        after = plugin_manager._build_config_text(before, enabled=True)
        self.assertIn("# keep this comment", after)
        self.assertIn("[profiles.dev]", after)
        self.assertIn('[plugins."other@personal"]', after)
        self.assertIn('[plugins."codex-memory@personal"]', after)
        parsed = plugin_manager._validate_toml(after)
        self.assertTrue(parsed["plugins"]["codex-memory@personal"]["enabled"])

    def test_install_plan_reports_diff_without_writing(self):
        plan = plugin_manager._install_plan(Path(".").resolve(), enabled=True, show_diff=True)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["action"], "install")
        self.assertIn("config_diff", plan)

    def test_install_plan_does_not_copy_when_source_is_install_path(self):
        plan = plugin_manager._install_plan(plugin_manager.PLUGIN_INSTALL_PATH, enabled=True, show_diff=False)
        self.assertFalse(plan["will_copy_files"])
