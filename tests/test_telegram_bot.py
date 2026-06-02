import importlib
import os
import sys
import types
import unittest
import asyncio
from unittest.mock import patch


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
            def __init__(self) -> None:
                self.concurrent_updates_value = None

            def token(self, token: str):
                self.token_value = token
                return self

            def concurrent_updates(self, value: bool):
                self.concurrent_updates_value = value
                return self

            def build(self):
                app = FakeApplication()
                app.concurrent_updates_value = self.concurrent_updates_value
                return app

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
                "discuss",
                "approve",
                "showplan",
                "history",
                "verbosity",
                "implement",
                "bugfix",
                "answer",
                "confirm",
                "cancel",
                "ci",
                "fixpr",
                "diff",
                "show",
                "pr",
                "limits",
                "codex",
                "test",
                "branches",
                "status",
                "logs",
            ],
        )
        self.assertEqual(len(app.error_handlers), 1)
        self.assertIs(app.concurrent_updates_value, True)

    def test_redact_sensitive_replaces_configured_secrets(self) -> None:
        config = importlib.import_module("ai_agent.config")

        redacted = config.redact_sensitive("telegram-secret anthropic-secret visible")

        self.assertEqual(redacted, "[redacted] [redacted] visible")

    def test_active_execution_text_reports_running_phase(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")
        context = types.SimpleNamespace(user_data={})

        telegram_bot.set_active_execution(context, "bugfix/example", "Running Codex")

        self.assertEqual(
            telegram_bot.active_execution_text(context),
            "Implementation status:\nRUNNING\n\nBranch:\nbugfix/example\n\nPhase:\nRunning Codex",
        )

    def test_build_ci_repair_prompt_includes_original_prompt_and_failure_context(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        prompt = telegram_bot.build_ci_repair_prompt("original task", "e: compile failed")

        self.assertIn("original task", prompt)
        self.assertIn("e: compile failed", prompt)
        self.assertIn("Do not create a new branch", prompt)

    def test_build_fix_pr_repair_prompt_includes_pr_and_failure_context(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        prompt = telegram_bot.build_fix_pr_repair_prompt(7, "Fix player", "body text", "compile failed")

        self.assertIn("#7 Fix player", prompt)
        self.assertIn("body text", prompt)
        self.assertIn("compile failed", prompt)
        self.assertIn("Do not create a new branch", prompt)

    def test_confirm_reports_existing_active_execution(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123), message=message)
        context = types.SimpleNamespace(
            user_data={
                "active_execution": {
                    "branch": "bugfix/example",
                    "phase": "Running Codex",
                    "status": "RUNNING",
                }
            }
        )

        asyncio.run(telegram_bot.confirm(update, context))

        self.assertEqual(len(message.replies), 1)
        self.assertIn("already running", message.replies[0])
        self.assertIn("bugfix/example", message.replies[0])

    def test_confirm_repairs_failed_ci_and_polls_repair_commit(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        async def fake_watch_ci(_update, head_sha):
            watched_shas.append(head_sha)
            if head_sha == "initial-sha":
                return types.SimpleNamespace(state="failed", summary="CI failed", url="https://example.test/run")
            return types.SimpleNamespace(state="passed", summary="CI passed", url="https://example.test/run2")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        watched_shas = []
        message = FakeMessage()
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123), message=message)
        context = types.SimpleNamespace(
            user_data={
                "pending_implementation": {
                    "change": "fix build",
                    "codex_prompt": "original prompt",
                    "branch_name": "bugfix/fix-build",
                    "commit_type": "fix",
                    "pr_body_label": "Bug fix prompt",
                    "confirmation_label": "bug fix",
                }
            }
        )

        original_watch_ci = telegram_bot.watch_ci
        telegram_bot.watch_ci = fake_watch_ci
        try:
            with (
                patch.object(telegram_bot, "CI_FIX_ATTEMPTS", 1),
                patch.object(telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread),
                patch.object(telegram_bot, "ensure_github_configured"),
                patch.object(
                    telegram_bot,
                    "implement",
                    return_value=types.SimpleNamespace(files_changed=["App.kt"], diff="diff1", output="implemented"),
                ),
                patch.object(telegram_bot, "push", side_effect=["initial-sha", "repair-sha"]) as mock_push,
                patch.object(
                    telegram_bot,
                    "create_pull_request",
                    return_value=types.SimpleNamespace(number=42, url="https://example.test/pr", head_sha="stale-or-pr-sha"),
                ),
                patch.object(telegram_bot, "build_failure_context", return_value="compile failed") as mock_failure_context,
                patch.object(
                    telegram_bot,
                    "repair_implementation",
                    return_value=types.SimpleNamespace(files_changed=["App.kt"], diff="diff2", output="repaired"),
                ) as mock_repair,
            ):
                asyncio.run(telegram_bot.confirm(update, context))
        finally:
            telegram_bot.watch_ci = original_watch_ci

        self.assertEqual(watched_shas, ["initial-sha", "repair-sha"])
        self.assertEqual(mock_push.call_count, 2)
        mock_failure_context.assert_called_once()
        mock_repair.assert_called_once()
        self.assertNotIn("pending_implementation", context.user_data)
        self.assertEqual(context.user_data["last_execution"].tests, "PASS")

    def test_fixpr_repairs_same_repository_pr_and_polls_repair_commit(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        async def fake_watch_ci(_update, head_sha):
            watched_shas.append(head_sha)
            if head_sha == "initial-sha":
                return types.SimpleNamespace(state="failed", summary="CI failed", url="https://example.test/run")
            return types.SimpleNamespace(state="passed", summary="CI passed", url="https://example.test/run2")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        watched_shas = []
        message = FakeMessage()
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123), message=message)
        context = types.SimpleNamespace(args=["7"], user_data={})
        pull_data = {
            "number": 7,
            "state": "open",
            "html_url": "https://example.test/pr/7",
            "title": "Fix player",
            "body": "PR body",
            "head": {
                "ref": "bugfix/player",
                "sha": "initial-sha",
                "repo": {"full_name": telegram_bot.GITHUB_REPOSITORY},
            },
        }

        original_watch_ci = telegram_bot.watch_ci
        telegram_bot.watch_ci = fake_watch_ci
        try:
            with (
                patch.object(telegram_bot, "CI_FIX_ATTEMPTS", 1),
                patch.object(telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread),
                patch.object(telegram_bot, "ensure_github_configured"),
                patch.object(telegram_bot, "github_request", return_value=pull_data),
                patch.object(telegram_bot, "build_failure_context", return_value="compile failed") as mock_failure_context,
                patch.object(
                    telegram_bot,
                    "repair_pull_request_branch",
                    return_value=types.SimpleNamespace(files_changed=["App.kt"], diff="diff", output="repaired"),
                ) as mock_repair,
                patch.object(telegram_bot, "push", return_value="repair-sha") as mock_push,
            ):
                asyncio.run(telegram_bot.fixpr(update, context))
        finally:
            telegram_bot.watch_ci = original_watch_ci

        self.assertEqual(watched_shas, ["initial-sha", "repair-sha"])
        mock_failure_context.assert_called_once()
        mock_repair.assert_called_once()
        mock_push.assert_called_once_with("bugfix/player", "PR #7 CI repair", "fix")
        self.assertEqual(context.user_data["last_execution"].tests, "PASS")


if __name__ == "__main__":
    unittest.main()
