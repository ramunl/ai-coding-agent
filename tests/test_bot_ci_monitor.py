"""Behavior tests for bot ci_monitor."""

import asyncio
import importlib
import types
from unittest.mock import AsyncMock, patch

from tests.bot_fixtures import TelegramTestCase


class CiMonitorTests(TelegramTestCase):
    def test_pull_request_choices_filter_forks_for_repair(self):
        bot = importlib.import_module("ai_agent.bot.ci_monitor")
        message = types.SimpleNamespace(reply_text=AsyncMock())
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=[], user_data={})
        pulls = [
            {"number": 1, "head": {"repo": {"full_name": "owner/repo"}}},
            {"number": 2, "head": {"repo": {"full_name": "fork/repo"}}},
        ]
        with (
            patch.object(
                bot,
                "active_project",
                return_value=types.SimpleNamespace(github_repository="owner/repo"),
            ),
            patch.object(bot, "ensure_github_configured"),
            patch.object(bot, "github_request", return_value=pulls),
            patch.object(bot, "choice_keyboard", return_value=object()) as choices,
        ):
            asyncio.run(
                importlib.import_module("ai_agent.bot.execution").fixpr(update, context)
            )
            choices.assert_called_with("fixpr", ["1"])
            asyncio.run(bot.ci(update, context))
            choices.assert_called_with("ci", ["1", "2"])

    def test_build_ci_repair_prompt_includes_original_prompt_and_failure_context(
        self,
    ) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.ci_monitor")

        prompt = telegram_bot.build_ci_repair_prompt(
            "original task", "e: compile failed"
        )

        self.assertIn("original task", prompt)
        self.assertIn("e: compile failed", prompt)
        self.assertIn("Do not create a new branch", prompt)

    def test_build_fix_pr_repair_prompt_includes_pr_and_failure_context(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.ci_monitor")

        prompt = telegram_bot.build_fix_pr_repair_prompt(
            7, "Fix player", "body text", "compile failed"
        )

        self.assertIn("#7 Fix player", prompt)
        self.assertIn("body text", prompt)
        self.assertIn("compile failed", prompt)
        self.assertIn("Do not create a new branch", prompt)
