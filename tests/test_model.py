import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ModelErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "t"
        os.environ["YOUR_CHAT_ID"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "k"
        for module in ("ai_agent.config", "ai_agent.model_errors"):
            sys.modules.pop(module, None)
        self.model_errors = importlib.import_module("ai_agent.model_errors")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    def test_detects_model_not_found(self) -> None:
        body = '{"type":"error","error":{"type":"not_found_error","message":"model: claude-sonnet-4-20250514"}}'
        self.assertTrue(self.model_errors.is_model_not_found(404, body))

    def test_ignores_other_404s(self) -> None:
        body = '{"type":"error","error":{"type":"not_found_error","message":"unknown url"}}'
        self.assertFalse(self.model_errors.is_model_not_found(404, body))

    def test_ignores_non_404(self) -> None:
        body = '{"error":{"type":"overloaded_error"}}'
        self.assertFalse(self.model_errors.is_model_not_found(529, body))

    def test_handles_non_json_body(self) -> None:
        self.assertFalse(self.model_errors.is_model_not_found(404, "gateway timeout"))

    def test_message_names_the_model(self) -> None:
        msg = self.model_errors.model_error_message("claude-old-1")
        self.assertIn("claude-old-1", msg)
        self.assertIn("ANTHROPIC_MODEL", msg)


class ModelManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "t"
        os.environ["YOUR_CHAT_ID"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "k"
        self.tmp = Path(tempfile.mkdtemp())
        self.env_file = self.tmp / "ai-agent.env"
        os.environ["AGENT_ENV_FILE"] = str(self.env_file)
        for module in ("ai_agent.config", "ai_agent.model_errors",
                       "ai_agent.anthropic_limits", "ai_agent.model_manager"):
            sys.modules.pop(module, None)
        self.mm = importlib.import_module("ai_agent.model_manager")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    def test_rejects_malformed_name_without_api_call(self) -> None:
        with patch.object(self.mm, "_probe") as probe:
            ok, detail = self.mm.verify_model("bad name; rm -rf /")
        self.assertFalse(ok)
        probe.assert_not_called()  # never even hit the API

    def test_verify_reachable_model(self) -> None:
        with patch.object(self.mm, "_probe", return_value=(200, {}, "{}")):
            ok, detail = self.mm.verify_model("claude-sonnet-4-6")
        self.assertTrue(ok)

    def test_verify_retired_model(self) -> None:
        body = '{"error":{"type":"not_found_error","message":"model: x"}}'
        with patch.object(self.mm, "_probe", return_value=(404, {}, body)):
            ok, detail = self.mm.verify_model("claude-retired")
        self.assertFalse(ok)
        self.assertIn("retired", detail)

    def test_set_model_creates_line(self) -> None:
        self.env_file.write_text("TELEGRAM_BOT_TOKEN=t\nANTHROPIC_API_KEY=k\n")
        self.mm.set_model_in_env("claude-sonnet-4-6")
        content = self.env_file.read_text()
        self.assertIn("ANTHROPIC_MODEL=claude-sonnet-4-6", content)
        # existing lines preserved
        self.assertIn("TELEGRAM_BOT_TOKEN=t", content)

    def test_set_model_replaces_existing_line(self) -> None:
        self.env_file.write_text("ANTHROPIC_MODEL=old-model\nOTHER=1\n")
        self.mm.set_model_in_env("claude-sonnet-4-6")
        content = self.env_file.read_text()
        self.assertIn("ANTHROPIC_MODEL=claude-sonnet-4-6", content)
        self.assertNotIn("old-model", content)
        self.assertEqual(content.count("ANTHROPIC_MODEL="), 1)  # no duplicate
        self.assertIn("OTHER=1", content)


if __name__ == "__main__":
    unittest.main()
