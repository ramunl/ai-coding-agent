"""Behavior tests for bot providers."""

import asyncio
import importlib
import types
from unittest.mock import AsyncMock, patch

from tests.bot_fixtures import TelegramTestCase


class ProvidersTests(TelegramTestCase):
    def test_planner_without_argument_shows_choice_grid(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.providers")
        message = types.SimpleNamespace(reply_text=AsyncMock())
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=[], user_data={})
        keyboard = object()

        with patch.object(
            telegram_bot, "choice_keyboard", return_value=keyboard
        ) as choices:
            asyncio.run(telegram_bot.planner_cmd(update, context))

        choices.assert_called_once_with("planner", ["codex", "claude"], active="codex")
        self.assertIs(message.reply_text.await_args.kwargs["reply_markup"], keyboard)

    def test_agent_command_sets_implementation_agent(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.providers")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=["claude"], user_data={})

        asyncio.run(telegram_bot.agent_cmd(update, context))

        self.assertEqual(context.user_data["implementation_agent"], "claude")
        self.assertIn("Claude", message.replies[0])

    def test_planner_command_sets_planning_agent(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.providers")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=["claude"], user_data={})

        asyncio.run(telegram_bot.planner_cmd(update, context))

        self.assertEqual(context.user_data["planning_agent"], "claude")
        self.assertIn("Claude", message.replies[0])

    def test_limits_all_shows_codex_and_claude(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.providers")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=[], user_data={})

        with patch.object(
            telegram_bot, "get_codex_status", return_value="Codex status"
        ):
            with patch.object(
                telegram_bot, "get_anthropic_limits", return_value="Claude limits"
            ):
                asyncio.run(telegram_bot.limits(update, context))

        output = "\n".join(message.replies)
        self.assertIn("Codex status", output)
        self.assertIn("Claude limits", output)

    def test_limits_planner_uses_selected_provider(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.providers")

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
            args=["planner"], user_data={"planning_agent": "codex"}
        )

        with patch.object(
            telegram_bot, "get_codex_status", return_value="Selected Codex"
        ):
            asyncio.run(telegram_bot.limits(update, context))

        self.assertIn("Selected Codex", message.replies[0])

    def test_model_selection_verifies_before_saving_and_restart(self) -> None:
        providers = importlib.import_module("ai_agent.bot.providers")
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123))
        context = types.SimpleNamespace(args=["claude", "set", "candidate"])
        actions = []
        tool = types.SimpleNamespace(
            name="claude",
            manageable=True,
            verify=lambda model: actions.append(("verify", model)) or (True, "ok"),
            set_model=lambda model: actions.append(("save", model)),
        )
        with (
            patch.object(providers, "get_tool", return_value=tool),
            patch.object(providers, "reply_chunks", AsyncMock()) as replies,
            patch.object(
                providers,
                "schedule_restart",
                side_effect=lambda: (
                    actions.append(("restart", None)) or "Restart scheduled"
                ),
            ),
        ):
            asyncio.run(providers.model(update, context))

        self.assertEqual(
            actions, [("verify", "candidate"), ("save", "candidate"), ("restart", None)]
        )
        self.assertIn("Verified and saved", replies.await_args.args[1])

    def test_model_selection_rejects_unreachable_model_without_saving(self) -> None:
        providers = importlib.import_module("ai_agent.bot.providers")
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123))
        context = types.SimpleNamespace(args=["claude", "set", "candidate"])
        from unittest.mock import Mock

        tool = Mock(name="tool")
        tool.name = "claude"
        tool.manageable = True
        tool.verify.return_value = (False, "unreachable")
        with (
            patch.object(providers, "get_tool", return_value=tool),
            patch.object(providers, "reply_chunks", AsyncMock()) as replies,
            patch.object(providers, "schedule_restart") as restart,
        ):
            asyncio.run(providers.model(update, context))

        tool.set_model.assert_not_called()
        restart.assert_not_called()
        self.assertIn("Refusing to switch", replies.await_args.args[1])
