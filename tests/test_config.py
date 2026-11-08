import os
import unittest
from unittest.mock import patch

from ai_agent.config import parse_size, validate_required_config


class ParseSizeTests(unittest.TestCase):
    def test_plain_bytes_and_decimal_units(self) -> None:
        self.assertEqual(parse_size("5000000000"), 5_000_000_000)
        self.assertEqual(parse_size("5GB"), 5_000_000_000)
        self.assertEqual(parse_size(" 10 mb "), 10_000_000)

    def test_binary_units(self) -> None:
        self.assertEqual(parse_size("500MiB"), 500 * 1024 * 1024)
        self.assertEqual(parse_size("2gib"), 2 * 1024**3)

    def test_rejects_unknown_units_negatives_and_fractions(self) -> None:
        for text in ("", "5 XB", "-1GB", "1.5GB", "five"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, "Invalid size"):
                    parse_size(text)


class ConfigTests(unittest.TestCase):
    def test_validate_required_config_reports_missing_values(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                RuntimeError, "TELEGRAM_BOT_TOKEN, YOUR_CHAT_ID"
            ):
                validate_required_config()

    def test_validate_required_config_accepts_required_values(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "YOUR_CHAT_ID": "123",
            "ANTHROPIC_API_KEY": "anthropic-secret",
        }
        with patch.dict(os.environ, env, clear=True):
            validate_required_config()

    def test_validate_required_config_allows_codex_without_anthropic_key(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "YOUR_CHAT_ID": "123",
            "PLANNING_AGENT": "codex",
        }
        with patch.dict(os.environ, env, clear=True):
            validate_required_config()

    def test_validate_required_config_requires_key_for_default_claude_planner(
        self,
    ) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "YOUR_CHAT_ID": "123",
            "PLANNING_AGENT": "claude",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY is required"):
                validate_required_config()

    def test_validate_required_config_rejects_unknown_implementation_agent(
        self,
    ) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "YOUR_CHAT_ID": "123",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "IMPLEMENTATION_AGENT": "other",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(
                RuntimeError, "IMPLEMENTATION_AGENT must be codex or claude"
            ):
                validate_required_config()

    def test_validate_work_config_accepts_test_defaults(self) -> None:
        from ai_agent import config

        config.validate_work_config()

    def test_validate_work_config_rejects_root_worker(self) -> None:
        from ai_agent import config

        with patch.object(config, "WORKER_USER", "root"):
            with self.assertRaisesRegex(RuntimeError, "WORKER_USER"):
                config.validate_work_config()

    def test_validate_work_config_rejects_relative_work_root(self) -> None:
        from ai_agent import config

        with patch.object(config, "WORK_ROOT", config.Path("relative/work")):
            with self.assertRaisesRegex(RuntimeError, "WORK_ROOT"):
                config.validate_work_config()

    def test_validate_work_config_rejects_work_root_inside_git(self) -> None:
        from ai_agent import config

        repo_root = config.Path(__file__).resolve().parents[1]
        with patch.object(config, "WORK_ROOT", repo_root / "work"):
            with self.assertRaisesRegex(RuntimeError, "WORK_ROOT"):
                config.validate_work_config()


if __name__ == "__main__":
    unittest.main()
