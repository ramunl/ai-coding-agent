"""Behavior tests for bot application."""

import asyncio
import importlib
import types
from unittest.mock import AsyncMock, patch

from tests.bot_fixtures import TelegramTestCase


class ApplicationTests(TelegramTestCase):
    def test_build_application_registers_expected_commands(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        app = telegram_bot.build_application()
        commands = [
            handler.command for handler in app.handlers if hasattr(handler, "command")
        ]

        self.assertEqual(
            commands,
            [
                "start",
                "help",
                "more",
                "version",
                "plan",
                "discuss",
                "approve",
                "showplan",
                "history",
                "verbosity",
                "implement",
                "bugfix",
                "answer",
                "confirm",
                "queue",
                "planner",
                "agent",
                "cancel",
                "ci",
                "fixpr",
                "diff",
                "show",
                "pr",
                "limits",
                "model",
                "core",
                "codex",
                "test",
                "pull",
                "repo_list",
                "repo_add",
                "repo_use",
                "repo_remove",
                "branches",
                "branch",
                "deploy",
                "status",
                "logs",
            ],
        )
        self.assertEqual(len(app.error_handlers), 1)
        self.assertIs(app.concurrent_updates_value, True)
        self.assertIs(app.post_init_value, telegram_bot.configure_bot_commands)

    def test_project_commands_appear_in_autocomplete_menu(self) -> None:
        """Registering a handler is not enough: it must also be in BOT_COMMANDS."""
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        menu = [command.command for command in telegram_bot.BOT_COMMANDS]

        for name in ("repo_list", "repo_add", "repo_use", "repo_remove"):
            self.assertIn(name, menu)

    def test_help_and_more_send_standard_messages_with_all_command_buttons(self):
        bot = importlib.import_module("ai_agent.telegram_bot")
        expected = {"command:" + c.command for c in bot.BOT_COMMANDS}
        expected.update({"command:core update", "command:core release"})
        for handler in (bot.start, bot.more):
            update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123))
            context = types.SimpleNamespace(
                user_data={}, bot=types.SimpleNamespace(send_message=AsyncMock())
            )
            asyncio.run(handler(update, context))
            payload = context.bot.send_message.await_args.kwargs
            self.assertEqual(payload["chat_id"], 123)
            self.assertIsNone(payload["parse_mode"])
            self.assertLess(len(payload["text"]), 4096)
            self.assertIn("/version", payload["text"])
            rows = payload["reply_markup"].inline_keyboard
            self.assertTrue(all(len(row) <= 3 for row in rows))
            self.assertEqual(
                {button.callback_data for row in rows for button in row}, expected
            )
            for row in rows:
                for button in row:
                    self.assertLessEqual(len(button.callback_data.encode()), 64)
                    self.assertIs(
                        bot._callback_router.resolve(button.callback_data)[0],
                        bot._on_command_tap,
                    )

    def test_command_buttons_execute_same_handlers_as_typed_commands(self):
        bot = importlib.import_module("ai_agent.telegram_bot")
        app = bot.build_application()
        typed = {h.command: h.callback for h in app.handlers if hasattr(h, "command")}
        self.assertEqual(typed, bot.command_handlers())
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123), reply_text=AsyncMock()
        )
        query = types.SimpleNamespace(
            data="command:version", answer=AsyncMock(), message=message
        )
        update = types.SimpleNamespace(
            update_id=1, effective_chat=message.chat, callback_query=query
        )
        context = types.SimpleNamespace(args=["stale"], user_data={})
        with patch(
            "ai_agent.bot.maintenance.get_runtime_version",
            return_value="shared version",
        ):
            asyncio.run(bot._callback_router.dispatch(update, context))
        message.reply_text.assert_awaited_once_with("shared version")
        query.answer.assert_awaited_once()
        query.data = "command:planner"
        asyncio.run(bot._callback_router.dispatch(update, context))
        rows = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(
            [b.callback_data for row in rows for b in row],
            ["planner:codex", "planner:claude"],
        )
        self.assertEqual(context.args, ["stale"])
        query.data = "command:core update"
        with patch.object(bot, "core", new_callable=AsyncMock) as core:
            asyncio.run(bot._callback_router.dispatch(update, context))
            self.assertEqual(core.await_args.args[1].args, ["update"])
        message.reply_text.reset_mock()
        update.effective_chat = types.SimpleNamespace(id=999)
        asyncio.run(bot._callback_router.dispatch(update, context))
        message.reply_text.assert_not_awaited()

    def test_pull_request_callback_reuses_handler_and_answers_query(self):
        bot = importlib.import_module("ai_agent.telegram_bot")
        message = types.SimpleNamespace(chat=types.SimpleNamespace(id=123))
        query = types.SimpleNamespace(
            data="fixpr:19", answer=AsyncMock(), message=message
        )
        update = types.SimpleNamespace(
            update_id=1, effective_chat=message.chat, callback_query=query
        )
        context = types.SimpleNamespace(args=["old"], user_data={})
        with patch.object(bot, "fixpr", new_callable=AsyncMock) as handler:
            asyncio.run(bot._callback_router.dispatch(update, context))
            query.answer.assert_awaited_once()
            self.assertIs(handler.await_args.args[0].message, message)
            self.assertEqual(handler.await_args.args[1].args, ["19"])
            self.assertEqual(context.args, ["old"])
            update.effective_chat = types.SimpleNamespace(id=999)
            asyncio.run(bot._callback_router.dispatch(update, context))
            self.assertEqual(handler.await_count, 1)

    def test_free_text_requires_reply_to_prompt_and_cancel_clears_it(self):
        bot = importlib.import_module("ai_agent.telegram_bot")
        message = types.SimpleNamespace(
            reply_text=AsyncMock(return_value=types.SimpleNamespace(message_id=10))
        )
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=[], user_data={})
        asyncio.run(bot.plan(update, context))
        self.assertEqual(context.user_data["argument_prompt"], (10, "plan"))
        message.text = "Build a dashboard"
        message.reply_to_message = types.SimpleNamespace(message_id=9)
        with patch.object(bot, "plan", new_callable=AsyncMock) as handler:
            asyncio.run(bot.argument_reply(update, context))
            handler.assert_not_awaited()
            message.reply_to_message.message_id = 10
            asyncio.run(bot.argument_reply(update, context))
            self.assertEqual(
                handler.await_args.args[1].args, ["Build", "a", "dashboard"]
            )
            self.assertNotIn("argument_prompt", context.user_data)
        context.user_data["argument_prompt"] = (11, "implement")
        asyncio.run(bot.cancel(update, context))
        self.assertNotIn("argument_prompt", context.user_data)

    def test_configure_bot_commands_includes_fixpr(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")
        app = telegram_bot.build_application()

        with patch.object(
            telegram_bot,
            "_notify_core_drift_on_startup",
            new=AsyncMock(),
        ):
            asyncio.run(telegram_bot.configure_bot_commands(app))

        command_names = [command.command for command in app.commands]
        self.assertIn("fixpr", command_names)
        self.assertIn("queue", command_names)
        self.assertIn("planner", command_names)
        self.assertIn("agent", command_names)

    def test_startup_reports_queue_restored_after_restart(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")
        app = telegram_bot.build_application()
        app.bot.send_message = AsyncMock()
        app.user_data = {123: {"task_queue": [{"id": 1, "branch_name": "feature/a"}]}}

        with patch.object(asyncio, "to_thread", new=AsyncMock(return_value=None)):
            asyncio.run(telegram_bot.configure_bot_commands(app))

        app.bot.send_message.assert_awaited_once()
        text = app.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Restored 1 queued task(s)", text)
        self.assertIn("/confirm", text)

    def test_startup_is_silent_when_nothing_was_queued(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")
        app = telegram_bot.build_application()
        app.bot.send_message = AsyncMock()
        app.user_data = {123: {}}

        with patch.object(asyncio, "to_thread", new=AsyncMock(return_value=None)):
            asyncio.run(telegram_bot.configure_bot_commands(app))

        app.bot.send_message.assert_not_awaited()

    def test_configure_bot_commands_sends_core_drift_notice(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")
        app = telegram_bot.build_application()
        app.bot.send_message = AsyncMock()

        with patch.object(
            asyncio,
            "to_thread",
            new=AsyncMock(return_value="core update available"),
        ):
            asyncio.run(telegram_bot.configure_bot_commands(app))

        app.bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text="core update available",
        )
