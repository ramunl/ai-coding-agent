import importlib
import os
import sys
import types
import unittest


class TelegramBotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = {
            "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
            "YOUR_CHAT_ID": os.environ.get("YOUR_CHAT_ID"),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
        }
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-secret"

        self.previous_modules = {
            name: sys.modules.get(name)
            for name in [
                "telegram",
                "telegram.ext",
                "anthropic",
                "ai_agent.config",
                "ai_agent.planner",
                "ai_agent.telegram_bot",
            ]
        }
        for name in self.previous_modules:
            sys.modules.pop(name, None)

        telegram_module = types.ModuleType("telegram")
        telegram_module.Update = type("Update", (), {})

        ext_module = types.ModuleType("telegram.ext")

        class FakeApplication:
            def __init__(self) -> None:
                self.handlers = []
                self.error_handlers = []

            @classmethod
            def builder(cls):
                return FakeBuilder()

            def add_handler(self, handler) -> None:
                self.handlers.append(handler)

            def add_error_handler(self, handler) -> None:
                self.error_handlers.append(handler)

        class FakeBuilder:
            def token(self, token: str):
                self.token_value = token
                return self

            def build(self):
                return FakeApplication()

        class FakeCommandHandler:
            def __init__(self, command: str, callback) -> None:
                self.command = command
                self.callback = callback

        ext_module.Application = FakeApplication
        ext_module.CommandHandler = FakeCommandHandler
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

    def test_build_application_registers_expected_commands(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        app = telegram_bot.build_application()
        commands = [handler.command for handler in app.handlers]

        self.assertEqual(
            commands,
            [
                "start",
                "help",
                "plan",
                "implement",
                "bugfix",
                "confirm",
                "cancel",
                "ci",
                "limits",
                "codex",
                "test",
                "branches",
                "status",
                "logs",
            ],
        )
        self.assertEqual(len(app.error_handlers), 1)

    def test_redact_sensitive_replaces_configured_secrets(self) -> None:
        config = importlib.import_module("ai_agent.config")

        redacted = config.redact_sensitive("telegram-secret anthropic-secret visible")

        self.assertEqual(redacted, "[redacted] [redacted] visible")


if __name__ == "__main__":
    unittest.main()
