"""Behavior tests for bot menus."""

import asyncio
import importlib
import types
from unittest.mock import AsyncMock

from tests.bot_fixtures import TelegramTestCase


class MenusTests(TelegramTestCase):
    def test_more_carries_the_full_reference(self) -> None:
        """Detail removed from /help must still be reachable via /more."""
        telegram_bot = importlib.import_module("ai_agent.bot.menus")

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=object()
        )
        context = types.SimpleNamespace(
            user_data={}, bot=types.SimpleNamespace(send_message=AsyncMock())
        )

        asyncio.run(telegram_bot.more(update, context))

        more_text = context.bot.send_message.await_args.kwargs["text"]

        for detail in (
            "/planner",
            "/agent",
            "/fixpr",
            "/verbosity",
            "/showplan",
            "/core release",
            "/repo_add",
            "active project",
        ):
            self.assertIn(detail, more_text)
