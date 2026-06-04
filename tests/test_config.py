import os
import unittest
from unittest.mock import patch

from ai_agent.config import validate_required_config


class ConfigTests(unittest.TestCase):
    def test_validate_required_config_reports_missing_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_BOT_TOKEN, YOUR_CHAT_ID, ANTHROPIC_API_KEY"):
                validate_required_config()

    def test_validate_required_config_accepts_required_values(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "YOUR_CHAT_ID": "123",
            "ANTHROPIC_API_KEY": "anthropic-secret",
        }
        with patch.dict(os.environ, env, clear=True):
            validate_required_config()

    def test_validate_required_config_rejects_unknown_implementation_agent(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "YOUR_CHAT_ID": "123",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "IMPLEMENTATION_AGENT": "other",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "IMPLEMENTATION_AGENT must be codex or claude"):
                validate_required_config()


if __name__ == "__main__":
    unittest.main()
