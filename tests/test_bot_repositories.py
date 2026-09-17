"""Behavior tests for bot repositories."""

import asyncio
import importlib
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.bot_fixtures import TelegramTestCase


class RepositoriesTests(TelegramTestCase):
    def test_repo_list_marks_only_the_active_repository(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")
        from ai_agent.projects import Project

        projects = [
            Project("alpha", Path("/srv/alpha"), "owner/alpha", "main", "alpha"),
            Project("beta", Path("/srv/beta"), "owner/beta", "develop", "beta"),
        ]
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=object()
        )
        context = types.SimpleNamespace(bot=types.SimpleNamespace(_post=AsyncMock()))

        with (
            patch.object(telegram_bot, "active_project", return_value=projects[1]),
            patch.object(telegram_bot, "list_projects", return_value=projects),
        ):
            asyncio.run(telegram_bot.repo_list(update, context))

        paragraphs = context.bot._post.await_args.kwargs["data"]["rich_message"][
            "blocks"
        ][1:3]
        self.assertNotIn("✓ ", paragraphs[0]["text"])
        self.assertEqual(paragraphs[1]["text"][0], "✓ ")
        self.assertNotIn(" — active", paragraphs[0]["text"])
        self.assertIn(" — active", paragraphs[1]["text"])

    def test_repo_list_preserves_special_characters_in_exact_payload(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")
        from ai_agent.projects import Project

        project = Project(
            "repo<&`*",
            Path("/srv/a <b> & `c`"),
            "owner/repo",
            "feature/<safe>&*",
            "rules",
        )
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=object()
        )
        context = types.SimpleNamespace(bot=types.SimpleNamespace(_post=AsyncMock()))

        with (
            patch.object(telegram_bot, "active_project", return_value=project),
            patch.object(telegram_bot, "list_projects", return_value=[project]),
        ):
            asyncio.run(telegram_bot.repo_list(update, context))

        context.bot._post.assert_awaited_once_with(
            "sendRichMessage",
            data={
                "chat_id": 123,
                "rich_message": {
                    "blocks": [
                        {"type": "heading", "size": 2, "text": "Repositories"},
                        {
                            "type": "paragraph",
                            "text": [
                                "✓ ",
                                {"type": "bold", "text": "repo<&`*"},
                                "\nPath: ",
                                {"type": "code", "text": "/srv/a <b> & `c`"},
                                "\nBranch: ",
                                {"type": "code", "text": "feature/<safe>&*"},
                                " — active",
                            ],
                        },
                        {
                            "type": "paragraph",
                            "text": [
                                "Switch with: ",
                                {"type": "code", "text": "/repo_use <name>"},
                            ],
                        },
                    ],
                    "skip_entity_detection": True,
                },
            },
        )

    def test_branch_shows_current_branch_without_args(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")

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

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            self.assertEqual(args, ["git", "branch", "--show-current"])
            return types.SimpleNamespace(output="main\n")

        with patch.object(telegram_bot, "run", fake_run):
            asyncio.run(telegram_bot.branch(update, context))

        self.assertIn("Current branch: main", message.replies[0])

    def test_branch_switches_to_requested_branch(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=["main"], user_data={})

        calls = []

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            calls.append(args)
            return types.SimpleNamespace(
                output="On branch main\nnothing to commit, working tree clean\n"
            )

        with patch.object(telegram_bot, "run", fake_run):
            asyncio.run(telegram_bot.branch(update, context))

        self.assertEqual(calls[0], ["git", "checkout", "main"])
        self.assertIn("Switched to branch: main", message.replies[0])

    def test_branch_falls_back_to_remote_when_local_checkout_fails(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=["feature/remote-only"], user_data={})

        calls = []

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            calls.append(args)
            if args == ["git", "checkout", "feature/remote-only"]:
                raise RuntimeError(
                    "Command failed (1): git checkout feature/remote-only\npathspec did not match"
                )
            return types.SimpleNamespace(output="On branch feature/remote-only\n")

        with patch.object(telegram_bot, "run", fake_run):
            asyncio.run(telegram_bot.branch(update, context))

        self.assertEqual(
            calls,
            [
                ["git", "checkout", "feature/remote-only"],
                ["git", "fetch", "origin", "feature/remote-only"],
                [
                    "git",
                    "checkout",
                    "-B",
                    "feature/remote-only",
                    "origin/feature/remote-only",
                ],
                ["git", "status"],
            ],
        )
        self.assertIn("Switched to branch: feature/remote-only", message.replies[0])

    def test_branch_blocked_while_implementation_running(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")

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
            args=["main"],
            user_data={
                "active_execution": {
                    "branch": "feature/running",
                    "phase": "Polling CI",
                    "status": "RUNNING",
                }
            },
        )

        asyncio.run(telegram_bot.branch(update, context))

        self.assertIn("An implementation is already running.", message.replies[0])

    def test_branch_rejects_invalid_branch_name(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=["../etc/passwd"], user_data={})

        asyncio.run(telegram_bot.branch(update, context))

        self.assertIn("Invalid branch name", message.replies[0])

    def test_pull_runs_git_pull_on_active_project(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(user_data={})

        calls = []

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            calls.append(args)
            return types.SimpleNamespace(output="Already up to date.\n")

        with patch.object(telegram_bot, "run", fake_run):
            asyncio.run(telegram_bot.pull(update, context))

        self.assertEqual(calls, [["git", "pull"]])
        self.assertIn("Already up to date.", message.replies[0])

    def test_pull_reports_git_failure(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(user_data={})

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            raise RuntimeError("Command failed (1): git pull\nCONFLICT")

        with patch.object(telegram_bot, "run", fake_run):
            asyncio.run(telegram_bot.pull(update, context))

        self.assertIn("git pull failed:", message.replies[0])
        self.assertIn("CONFLICT", message.replies[0])

    def test_pull_blocked_while_implementation_running(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.repositories")

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
            user_data={
                "active_execution": {
                    "branch": "feature/running",
                    "phase": "Polling CI",
                    "status": "RUNNING",
                }
            },
        )

        asyncio.run(telegram_bot.pull(update, context))

        self.assertIn("An implementation is already running.", message.replies[0])
