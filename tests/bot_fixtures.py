"""Isolated Telegram and provider fakes for bot command tests."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


class TelegramTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = {
            "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
            "YOUR_CHAT_ID": os.environ.get("YOUR_CHAT_ID"),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
            "AGENT_STATE_FILE": os.environ.get("AGENT_STATE_FILE"),
            "AGENT_SNAPSHOT_FILE": os.environ.get("AGENT_SNAPSHOT_FILE"),
        }
        # Tests must never write the real /var/lib state or dashboard files.
        self._state_dir = tempfile.mkdtemp()
        os.environ["AGENT_STATE_FILE"] = os.path.join(self._state_dir, "state.json")
        os.environ["AGENT_SNAPSHOT_FILE"] = os.path.join(
            self._state_dir, "snapshot.json"
        )
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-secret"

        self.previous_modules = {
            name: sys.modules.get(name)
            for name in ["ai_agent.bot"]
            + [
                "ai_agent.bot." + path.stem
                for path in (Path(__file__).resolve().parents[1] / "ai_agent/bot").glob(
                    "*.py"
                )
                if path.stem != "__init__"
            ]
            + [
                "telegram",
                "telegram.ext",
                "anthropic",
                "agent",
                "ai_agent.config",
                "ai_agent.planner",
                "ai_agent.telegram_bot",
            ]
        }
        for name in self.previous_modules:
            sys.modules.pop(name, None)

        telegram_module = types.ModuleType("telegram")
        telegram_module.BotCommand = lambda command, description: types.SimpleNamespace(
            command=command, description=description
        )
        telegram_module.Update = lambda update_id, message: types.SimpleNamespace(
            update_id=update_id, message=message, effective_chat=message.chat
        )
        telegram_module.InlineKeyboardButton = lambda text, callback_data: (
            types.SimpleNamespace(text=text, callback_data=callback_data)
        )
        telegram_module.InlineKeyboardMarkup = lambda rows: types.SimpleNamespace(
            inline_keyboard=rows
        )
        telegram_module.ForceReply = lambda **kwargs: types.SimpleNamespace(**kwargs)

        ext_module = types.ModuleType("telegram.ext")
        ext_module.ApplicationHandlerStop = type(
            "ApplicationHandlerStop", (Exception,), {}
        )

        class FakeApplication:
            def __init__(self) -> None:
                self.handlers = []
                self.error_handlers = []
                self.bot = types.SimpleNamespace(set_my_commands=self.set_my_commands)
                self.commands = None

            @classmethod
            def builder(cls):
                return FakeBuilder()

            def add_handler(self, handler) -> None:
                self.handlers.append(handler)

            def add_error_handler(self, handler) -> None:
                self.error_handlers.append(handler)

            async def set_my_commands(self, commands) -> None:
                self.commands = commands

        class FakeBuilder:
            def __init__(self) -> None:
                self.concurrent_updates_value = None
                self.post_init_value = None

            def token(self, token: str):
                self.token_value = token
                return self

            def concurrent_updates(self, value: bool):
                self.concurrent_updates_value = value
                return self

            def post_init(self, callback):
                self.post_init_value = callback
                return self

            def build(self):
                app = FakeApplication()
                app.concurrent_updates_value = self.concurrent_updates_value
                app.post_init_value = self.post_init_value
                return app

        class FakeCommandHandler:
            def __init__(self, command: str, callback) -> None:
                self.command = command
                self.callback = callback

        class FakeCallbackQueryHandler:
            def __init__(self, callback) -> None:
                self.callback = callback

        ext_module.Application = FakeApplication
        ext_module.CommandHandler = FakeCommandHandler
        ext_module.CallbackQueryHandler = FakeCallbackQueryHandler
        ext_module.MessageHandler = lambda filters, callback: types.SimpleNamespace(
            callback=callback
        )

        class FakeFilter:
            def __and__(self, other):
                return self

            def __invert__(self):
                return self

        ext_module.filters = types.SimpleNamespace(
            TEXT=FakeFilter(), COMMAND=FakeFilter()
        )
        ext_module.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)

        anthropic_module = types.ModuleType("anthropic")
        anthropic_module.Anthropic = lambda api_key: object()

        sys.modules["telegram"] = telegram_module
        sys.modules["telegram.ext"] = ext_module
        sys.modules["anthropic"] = anthropic_module

    def tearDown(self) -> None:
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        for name, module in self.previous_modules.items():
            sys.modules.pop(name, None)
            if module is not None:
                sys.modules[name] = module

    @staticmethod
    def _flatten_rich_text(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return TelegramTestCase._flatten_rich_text(value.get("text", ""))
        if isinstance(value, list):
            return "".join(TelegramTestCase._flatten_rich_text(item) for item in value)
        return ""
