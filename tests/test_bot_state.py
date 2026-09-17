"""Behavior tests for bot state."""

import importlib
import types

from tests.bot_fixtures import TelegramTestCase


class StateTests(TelegramTestCase):
    def test_active_execution_text_reports_running_phase(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.state")
        context = types.SimpleNamespace(user_data={})

        telegram_bot.set_active_execution(context, "bugfix/example", "Running Codex")

        self.assertEqual(
            telegram_bot.active_execution_text(context),
            "Implementation status:\nRUNNING\n\nBranch:\nbugfix/example\n\nPhase:\nRunning Codex",
        )
