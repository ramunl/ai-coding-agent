"""The autonomous-DeepSeek gate: off by default and only the worker consults it.

DeepSeek is not a wired provider yet (that is CTX03-CTX06); this covers the
forward-looking gate in ai_agent.worker.pick_tool and proves manual planning
never reads it. Importing the worker pulls in the planner chain, which imports
anthropic, so a fake anthropic is registered before the first import.
"""

import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from types import ModuleType

OWNER = 123
NOW = 1_800_000_000.0


def _fake_anthropic() -> ModuleType:
    module = types.ModuleType("anthropic")
    module.Anthropic = lambda api_key, **kwargs: object()
    module.DefaultHttpxClient = lambda **kwargs: object()
    module.NotFoundError = type("NotFoundError", (Exception,), {})
    return module


def _load_worker(enabled: bool | None) -> ModuleType:
    """Re-read config, then the worker, with the flag set/cleared as asked."""
    if enabled is None:
        os.environ.pop("AUTONOMOUS_DEEPSEEK_ENABLED", None)
    else:
        os.environ["AUTONOMOUS_DEEPSEEK_ENABLED"] = "true" if enabled else "false"
    for name in ("ai_agent.config", "ai_agent.worker"):
        sys.modules.pop(name, None)
    return importlib.import_module("ai_agent.worker")


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = str(OWNER)
        os.environ.pop("AUTONOMOUS_DEEPSEEK_ENABLED", None)
        self.previous_anthropic = sys.modules.get("anthropic")
        sys.modules["anthropic"] = _fake_anthropic()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)
        if self.previous_anthropic is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self.previous_anthropic

    def test_flag_defaults_to_off(self) -> None:
        worker = _load_worker(None)
        self.assertFalse(worker.AUTONOMOUS_DEEPSEEK_ENABLED)

    def test_flag_on_when_enabled(self) -> None:
        worker = _load_worker(True)
        self.assertTrue(worker.AUTONOMOUS_DEEPSEEK_ENABLED)

    def test_gate_off_blocks_autonomous_deepseek(self) -> None:
        worker = _load_worker(False)
        self.assertFalse(worker.autonomous_tool_allowed("deepseek"))
        # Without auto_tool there is no fallback, so nothing gets picked.
        tool, status = worker.pick_tool(
            "deepseek", {}, {"reserve": 30, "auto_tool": False}, NOW
        )
        self.assertIsNone(tool)
        self.assertIn("deepseek is gated off", status)
        # With auto_tool on, DeepSeek is skipped for a wired tool instead.
        tool, _ = worker.pick_tool(
            "deepseek", {}, {"reserve": 30, "auto_tool": True}, NOW
        )
        self.assertIn(tool, worker.TOOLS)

    def test_gate_on_allows_autonomous_deepseek(self) -> None:
        worker = _load_worker(True)
        self.assertTrue(worker.autonomous_tool_allowed("deepseek"))
        tool, note = worker.pick_tool(
            "deepseek", {}, {"reserve": 30, "auto_tool": False}, NOW
        )
        self.assertEqual((tool, note), ("deepseek", ""))

    def test_wired_tools_are_never_gated(self) -> None:
        worker = _load_worker(False)
        for tool in worker.TOOLS:
            with self.subTest(tool=tool):
                self.assertTrue(worker.autonomous_tool_allowed(tool))

    def test_manual_planning_path_does_not_consult_the_gate(self) -> None:
        planner = importlib.import_module("ai_agent.planner")
        provider = importlib.import_module("ai_agent.planning_provider")
        for module in (planner, provider):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("autonomous_tool_allowed", source)
            self.assertNotIn("AUTONOMOUS_DEEPSEEK_ENABLED", source)
            self.assertNotIn("ai_agent.worker", source)
        # Manual /plan normalization is provider-agnostic to the gate.
        self.assertEqual(provider.normalize_planning_agent("codex"), "codex")
        self.assertEqual(provider.normalize_planning_agent("claude"), "claude")


if __name__ == "__main__":
    unittest.main()
