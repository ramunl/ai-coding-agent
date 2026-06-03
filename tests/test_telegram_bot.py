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
                "agent",
                "ai_agent.config",
                "ai_agent.planner",
                "ai_agent.telegram_bot",
            ]
        }
        for name in self.previous_modules:
            sys.modules.pop(name, None)

        telegram_module = types.ModuleType("telegram")
        telegram_module.BotCommand = lambda command, description: types.SimpleNamespace(command=command, description=description)
        telegram_module.Update = type("Update", (), {})

        ext_module = types.ModuleType("telegram.ext")
        ext_module.ApplicationHandlerStop = type("ApplicationHandlerStop", (Exception,), {})

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
                "queue",
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
        self.assertIs(app.post_init_value, telegram_bot.configure_bot_commands)

    def test_help_text_includes_fixpr_description(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123), message=message)
        context = types.SimpleNamespace(user_data={})

        asyncio.run(telegram_bot.start(update, context))

        help_text = "\n".join(message.replies)
        self.assertIn("fixpr <pr-number>", help_text)
        self.assertIn("repair failed CI on an existing same-repository PR branch", help_text)
        self.assertIn("/queue", help_text)
        self.assertNotIn("/plan <feature>", help_text)
        self.assertNotIn("/fixpr <pr-number>", help_text)

    def test_entrypoint_help_text_includes_fixpr_description(self) -> None:
        agent = importlib.import_module("agent")

        self.assertIn("fixpr <pr-number>", agent.HELP_TEXT)
        self.assertIn("repair failed CI on an existing same-repository PR", agent.HELP_TEXT)
        self.assertIn("/queue", agent.HELP_TEXT)
        self.assertNotIn("/plan <feature>", agent.HELP_TEXT)
        self.assertNotIn("/fixpr <pr-number>", agent.HELP_TEXT)

    def test_configure_bot_commands_includes_fixpr(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")
        app = telegram_bot.build_application()

        asyncio.run(telegram_bot.configure_bot_commands(app))

        command_names = [command.command for command in app.commands]
        self.assertIn("fixpr", command_names)
        self.assertIn("queue", command_names)

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

    def test_queue_command_lists_running_and_pending_tasks(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123), message=message)
        context = types.SimpleNamespace(
            args=[],
            user_data={
                "active_execution": {"branch": "feature/running", "phase": "Polling CI", "status": "RUNNING"},
                "task_queue": [
                    {"id": 3, "branch_name": "feature/queued", "confirmation_label": "implementation"},
                ],
            },
        )

        asyncio.run(telegram_bot.queue_cmd(update, context))

        output = "\n".join(message.replies)
        self.assertIn("Running: feature/running", output)
        self.assertIn("#3 feature/queued", output)

    def test_cancel_removes_queued_task_by_id(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123), message=message)
        context = types.SimpleNamespace(
            args=["7"],
            user_data={
                "task_queue": [
                    {"id": 7, "branch_name": "feature/remove", "confirmation_label": "implementation"},
                    {"id": 8, "branch_name": "feature/keep", "confirmation_label": "implementation"},
                ]
            },
        )

        asyncio.run(telegram_bot.cancel(update, context))

        self.assertIn("Queued task #7 removed", message.replies[0])
        self.assertEqual([task["id"] for task in context.user_data["task_queue"]], [8])

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

    def test_confirm_queues_pending_work_when_runner_is_active(self) -> None:
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
                "pending_implementation": {
                    "change": "fix build",
                    "codex_prompt": "original prompt",
                    "branch_name": "bugfix/fix-build",
                    "commit_type": "fix",
                    "pr_body_label": "Bug fix prompt",
                    "confirmation_label": "bug fix",
                },
                "active_execution": {
                    "branch": "bugfix/example",
                    "phase": "Running Codex",
                    "status": "RUNNING",
                },
                "queue_runner_active": True,
            }
        )

        asyncio.run(telegram_bot.confirm(update, context))

        self.assertEqual(len(message.replies), 1)
        self.assertIn("Queued task #1", message.replies[0])
        self.assertEqual(context.user_data["task_queue"][0]["branch_name"], "bugfix/fix-build")
        self.assertNotIn("pending_implementation", context.user_data)

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
        joined_replies = "\n\n".join(message.replies)
        self.assertIn("CI passed", joined_replies)
        self.assertIn("Implementation completed.", joined_replies)

    def test_confirm_drains_existing_queue_before_new_task_fifo(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        async def fake_watch_ci(_update, head_sha):
            watched_shas.append(head_sha)
            return types.SimpleNamespace(state="passed", summary="CI passed", url=f"https://example.test/{head_sha}")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_implement(_prompt, branch_name):
            implemented_branches.append(branch_name)
            return types.SimpleNamespace(files_changed=[f"{branch_name}.kt"], diff="diff", output=f"implemented {branch_name}")

        implemented_branches = []
        watched_shas = []
        message = FakeMessage()
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123), message=message)
        context = types.SimpleNamespace(
            user_data={
                "next_task_id": 2,
                "task_queue": [
                    {
                        "id": 1,
                        "change": "first task",
                        "codex_prompt": "first prompt",
                        "branch_name": "feature/first",
                        "commit_type": "feat",
                        "pr_body_label": "Plan",
                        "confirmation_label": "implementation",
                    }
                ],
                "pending_implementation": {
                    "change": "second task",
                    "codex_prompt": "second prompt",
                    "branch_name": "feature/second",
                    "commit_type": "feat",
                    "pr_body_label": "Plan",
                    "confirmation_label": "implementation",
                },
            }
        )

        original_watch_ci = telegram_bot.watch_ci
        telegram_bot.watch_ci = fake_watch_ci
        try:
            with (
                patch.object(telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread),
                patch.object(telegram_bot, "ensure_github_configured"),
                patch.object(telegram_bot, "implement", side_effect=fake_implement),
                patch.object(telegram_bot, "push", side_effect=["first-sha", "second-sha"]),
                patch.object(
                    telegram_bot,
                    "create_pull_request",
                    side_effect=[
                        types.SimpleNamespace(number=1, url="https://example.test/pr/1", head_sha="first-sha"),
                        types.SimpleNamespace(number=2, url="https://example.test/pr/2", head_sha="second-sha"),
                    ],
                ),
            ):
                asyncio.run(telegram_bot.confirm(update, context))
        finally:
            telegram_bot.watch_ci = original_watch_ci

        self.assertEqual(implemented_branches, ["feature/first", "feature/second"])
        self.assertEqual(watched_shas, ["first-sha", "second-sha"])
        self.assertEqual(context.user_data["task_queue"], [])
        self.assertNotIn("queue_runner_active", context.user_data)
        self.assertIn("Queued task #2 at position 2", "\n".join(message.replies))

    def test_confirm_reports_failed_ci_after_repair_attempts_are_exhausted(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        async def fake_watch_ci(_update, head_sha):
            watched_shas.append(head_sha)
            return types.SimpleNamespace(state="failed", summary="CI failed", url=f"https://example.test/{head_sha}")

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
                patch.object(telegram_bot, "build_failure_context", return_value="compile failed"),
                patch.object(
                    telegram_bot,
                    "repair_implementation",
                    return_value=types.SimpleNamespace(files_changed=["App.kt"], diff="diff2", output="repaired"),
                ),
            ):
                asyncio.run(telegram_bot.confirm(update, context))
        finally:
            telegram_bot.watch_ci = original_watch_ci

        self.assertEqual(watched_shas, ["initial-sha", "repair-sha"])
        self.assertEqual(mock_push.call_count, 2)
        self.assertEqual(context.user_data["last_execution"].tests, "FAIL")
        joined_replies = "\n\n".join(message.replies)
        self.assertIn("CI is still failing after 1/1 repair attempts.", joined_replies)
        self.assertIn("Implementation failed.", joined_replies)
        self.assertNotIn("Implementation completed.", joined_replies)

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
