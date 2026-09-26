"""Pages persistence and eligibility tests without starting AstrBot."""
import ast
import importlib
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("cmdbridge_page_test")
package.__path__ = [str(ROOT)]
host = types.ModuleType("astrbot.api")
host.logger = logging.getLogger("cmdbridge-page-test")
web = types.ModuleType("astrbot.api.web")
web.request = types.SimpleNamespace(json=AsyncMock())
web.json_response = lambda data: types.SimpleNamespace(status_code=200, data=data)
web.error_response = lambda text, status_code=400: types.SimpleNamespace(status_code=status_code, data=text)
main = types.ModuleType("cmdbridge_page_test.main")
main._DEFAULT_EXCLUDES = {"help"}
with patch.dict(sys.modules, {"cmdbridge_page_test": package, "astrbot.api": host, "astrbot.api.web": web}):
    api = importlib.import_module("cmdbridge_page_test.page_api")


class Config(dict):
    def save_config(self):
        if getattr(self, "fail", False):
            raise OSError("disk full")
        self.saved = dict(self)


class PageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patch = patch.dict(sys.modules, {"cmdbridge_page_test.main": main})
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.config = Config(plugins=[], exclude_commands=["help"], overrides="{}", unrelated="keep")
        self.plugin = types.SimpleNamespace(context=Mock(), config=self.config,
            _refresh_bridges=AsyncMock(), describe_bridges=Mock(return_value={"commands": [], "plugins": [], "registered_count": 0}))
        self.page = api.BridgePageAPI(self.plugin)

    async def save(self, settings, revision=None):
        web.request.json = AsyncMock(return_value={"settings": settings,
            "revision": api.revision(self.page.document()) if revision is None else revision})
        return await self.page.save()

    async def test_save_persists_original_format_and_refreshes(self):
        response = await self.save({"plugins": ["plugin_a", "plugin_a"], "exclude_commands": [],
            "overrides": {"a_test": {"description": "说明", "params": {"query": "关键词"}}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config["plugins"], ["plugin_a"])
        self.assertEqual(self.config.saved["unrelated"], "keep")
        self.assertEqual(json.loads(self.config["overrides"])["a_test"]["params"]["query"], "关键词")
        self.plugin._refresh_bridges.assert_awaited_once()

    async def test_conflict_and_invalid_payload_do_not_refresh(self):
        valid = {"plugins": [], "exclude_commands": [], "overrides": {}}
        self.assertEqual((await self.save(valid, "stale")).status_code, 409)
        self.assertEqual((await self.save({**valid, "overrides": "{"})).status_code, 400)
        self.assertEqual((await self.save({**valid, "plugins": "a"})).status_code, 400)
        self.assertEqual((await self.save({**valid, "overrides": {"tool": {"params": []}}})).status_code, 400)
        self.plugin._refresh_bridges.assert_not_awaited()

    async def test_save_failure_rolls_back_and_does_not_refresh(self):
        self.config.fail = True
        with self.assertLogs("cmdbridge-page-test", level="ERROR"):
            response = await self.save({"plugins": ["new"], "exclude_commands": [], "overrides": {}})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.config["plugins"], [])
        self.plugin._refresh_bridges.assert_not_awaited()

    async def test_refresh_failure_reports_persisted_state(self):
        self.plugin._refresh_bridges.side_effect = RuntimeError("unavailable")
        with self.assertLogs("cmdbridge-page-test", level="ERROR"):
            response = await self.save({"plugins": ["new"], "exclude_commands": [], "overrides": {}})
        self.assertEqual(response.status_code, 500)
        self.assertIn("已保存", response.data)
        self.assertEqual(self.config.saved["plugins"], ["new"])

    async def test_inventory_read_never_refreshes(self):
        self.assertEqual((await self.page.settings()).status_code, 200)
        self.plugin._refresh_bridges.assert_not_awaited()


class EligibilityTests(unittest.TestCase):
    def test_display_reasons_and_filter_use_same_rules(self):
        # Compile the actual pure eligibility methods without importing the full host.
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "CmdBridgeStar")
        cls.bases = []
        cls.body = [item for item in cls.body if isinstance(item, ast.FunctionDef)
                    and item.name in ("_skip_reason", "_should_bridge_descriptor_static")]
        excluded = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_is_excluded_command")
        fragment = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_command_fragment")
        namespace = {"Any": object, "CommandGroupFilter": type("Group", (), {}), "star_map": {},
                     "_determine_permission": lambda handler: handler.permission}
        exec(compile(ast.Module(body=[fragment, excluded, cls], type_ignores=[]), "main.py", "exec"), namespace)
        target = namespace["CmdBridgeStar"]
        desc = types.SimpleNamespace(is_group=False, enabled=True, module_path="m", plugin_name="a",
            effective_command="test", handler_name="test_handler", filter_ref=None,
            handler=types.SimpleNamespace(permission="everyone"))
        self.assertTrue(target._should_bridge_descriptor_static(desc, whitelist={"a"}, exclude_commands=set()))
        for attribute, value, label in [("is_group", True, "指令组"), ("enabled", False, "禁用")]:
            old = getattr(desc, attribute); setattr(desc, attribute, value)
            self.assertIn(label, target._skip_reason(desc, {"a"}, set()))
            self.assertFalse(target._should_bridge_descriptor_static(desc, whitelist={"a"}, exclude_commands=set()))
            setattr(desc, attribute, old)
        desc.handler.permission = "admin"
        self.assertIn("管理员", target._skip_reason(desc, {"a"}, set()))
        desc.handler.permission = "everyone"
        self.assertIn("白名单", target._skip_reason(desc, set(), set()))
        self.assertIn("排除", target._skip_reason(desc, {"a"}, {"TEST"}))


if __name__ == "__main__":
    unittest.main()
