"""Behavior tests for bot state."""

import importlib
import types
from unittest.mock import patch

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

    def test_set_active_execution_records_progress_fields(self) -> None:
        import ai_agent.context_attempts as attempts

        telegram_bot = importlib.import_module("ai_agent.bot.state")
        attempt = types.SimpleNamespace(
            request=types.SimpleNamespace(task_id="task-9"),
            state={"attempt_id": "attempt-9", "started_at": 1234.5},
        )
        token = attempts.current_attempt.set(attempt)
        context = types.SimpleNamespace(user_data={})
        try:
            with patch.object(telegram_bot.time, "time", return_value=1300.0):
                telegram_bot.set_active_execution(context, "feature/a", "Running Codex")
        finally:
            attempts.current_attempt.reset(token)

        record = context.user_data["active_execution"]
        self.assertEqual(record["task_id"], "task-9")
        self.assertEqual(record["attempt_id"], "attempt-9")
        self.assertEqual(record["started_at"], 1234.5)
        self.assertEqual(record["last_activity"], 1300.0)
        self.assertEqual(record["stage"], "implement")

    def test_snapshot_publishes_progress_and_none_after_restart(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.state")

        # No active execution (as after a restart): no live run is fabricated.
        self.assertIsNone(telegram_bot.snapshot({})["progress"])

        data = {
            "active_execution": {
                "branch": "feature/a",
                "phase": "Polling CI",
                "status": "RUNNING",
                "stage": "ci",
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "started_at": 100.0,
                "last_activity": 190.0,
            }
        }
        with patch.object(telegram_bot.time, "time", return_value=200.0):
            snap = telegram_bot.snapshot(data)
        self.assertEqual(
            snap["progress"],
            {
                "task_id": "task-1",
                "attempt_id": "attempt-1",
                "stage": "ci",
                "elapsed": 100,
                "last_activity": 10,
                "finish_estimate": {"status": "insufficient_history", "samples": 0},
            },
        )
