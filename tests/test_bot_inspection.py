"""Behavior tests for bot inspection."""

import asyncio
import importlib
import types
from unittest.mock import AsyncMock, patch

from tests.bot_fixtures import TelegramTestCase


class InspectionTests(TelegramTestCase):
    def test_verbosity_without_argument_shows_choice_grid(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.inspection")
        message = types.SimpleNamespace(reply_text=AsyncMock())
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=[], user_data={})
        keyboard = object()

        with patch.object(
            telegram_bot, "choice_keyboard", return_value=keyboard
        ) as choices:
            asyncio.run(telegram_bot.verbosity(update, context))

        choices.assert_called_once_with(
            "verbosity", ["concise", "normal", "debug"], active="concise"
        )
        self.assertIs(message.reply_text.await_args.kwargs["reply_markup"], keyboard)

    def test_queue_command_lists_running_and_pending_tasks(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.inspection")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(
            args=[],
            user_data={
                "active_execution": {
                    "branch": "feature/running",
                    "phase": "Polling CI",
                    "status": "RUNNING",
                },
                "task_queue": [
                    {
                        "id": 3,
                        "branch_name": "feature/queued",
                        "confirmation_label": "implementation",
                    },
                ],
            },
        )

        asyncio.run(telegram_bot.queue_cmd(update, context))

        output = "\n".join(message.replies)
        self.assertIn("Running: feature/running", output)
        self.assertIn("#3 feature/queued", output)

    def test_cancel_removes_queued_task_by_id(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.inspection")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(
            args=["7"],
            user_data={
                "task_queue": [
                    {
                        "id": 7,
                        "branch_name": "feature/remove",
                        "confirmation_label": "implementation",
                    },
                    {
                        "id": 8,
                        "branch_name": "feature/keep",
                        "confirmation_label": "implementation",
                    },
                ]
            },
        )

        asyncio.run(telegram_bot.cancel(update, context))

        self.assertIn("Queued task #7 removed", message.replies[0])
        self.assertEqual([task["id"] for task in context.user_data["task_queue"]], [8])
