import importlib
import os
import sys
import unittest
from unittest.mock import patch


class AIToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "t"
        os.environ["YOUR_CHAT_ID"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "k"
        for module in ("ai_agent.config", "ai_agent.model_errors",
                       "ai_agent.anthropic_limits", "ai_agent.model_manager",
                       "ai_agent.ai_tools"):
            sys.modules.pop(module, None)
        self.ai_tools = importlib.import_module("ai_agent.ai_tools")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    def test_all_known_tools_present(self) -> None:
        self.assertEqual(set(self.ai_tools.known_tools()), {"claude", "codex", "claude-code"})

    def test_claude_is_manageable(self) -> None:
        claude = self.ai_tools.get_tool("claude")
        self.assertTrue(claude.manageable)

    def test_clis_are_read_only_and_explain_why(self) -> None:
        for name in ("codex", "claude-code"):
            tool = self.ai_tools.get_tool(name)
            self.assertFalse(tool.manageable)
            # A read-only tool must tell the user where its model really lives.
            self.assertTrue(tool.info().note)

    def test_unknown_tool_returns_none(self) -> None:
        self.assertIsNone(self.ai_tools.get_tool("gemini"))

    def test_all_info_covers_every_tool(self) -> None:
        infos = self.ai_tools.all_info()
        self.assertEqual(len(infos), 3)
        tools = {info.tool for info in infos}
        self.assertEqual(tools, {"claude", "codex", "claude-code"})

    def test_claude_delegates_to_model_manager(self) -> None:
        claude = self.ai_tools.get_tool("claude")
        with patch.object(self.ai_tools.model_manager, "verify_model", return_value=(True, "reachable")) as verify:
            ok, detail = claude.verify("claude-sonnet-4-6")
        self.assertTrue(ok)
        verify.assert_called_once()

    def test_claude_set_delegates_to_env_writer(self) -> None:
        claude = self.ai_tools.get_tool("claude")
        with patch.object(self.ai_tools.model_manager, "set_model_in_env") as setter:
            claude.set_model("claude-sonnet-4-6")
        setter.assert_called_once_with("claude-sonnet-4-6")

    def test_case_insensitive_lookup(self) -> None:
        self.assertIsNotNone(self.ai_tools.get_tool("CLAUDE"))
        self.assertIsNotNone(self.ai_tools.get_tool("  codex  "))


if __name__ == "__main__":
    unittest.main()
