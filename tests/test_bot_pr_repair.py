"""Behavior tests for existing pull request repair."""

import asyncio
import importlib
import types
from unittest.mock import AsyncMock, patch

from tests.bot_fixtures import TelegramTestCase


class PullRequestRepairTests(TelegramTestCase):
    def test_fixpr_repairs_same_repository_pr_and_polls_repair_commit(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.execution")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        async def fake_watch_ci(_update, head_sha):
            watched_shas.append(head_sha)
            if head_sha == "initial-sha":
                return types.SimpleNamespace(
                    state="failed", summary="CI failed", url="https://example.test/run"
                )
            return types.SimpleNamespace(
                state="passed", summary="CI passed", url="https://example.test/run2"
            )

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        watched_shas = []
        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
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
                "repo": {"full_name": telegram_bot.active_project().github_repository},
            },
        }

        original_watch_ci = telegram_bot.watch_ci
        telegram_bot.watch_ci = fake_watch_ci
        try:
            with (
                patch.object(telegram_bot, "CI_FIX_ATTEMPTS", 1),
                patch("ai_agent.bot.ci_monitor.CI_FIX_ATTEMPTS", 1),
                patch.object(
                    telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread
                ),
                patch.object(telegram_bot, "ensure_github_configured"),
                patch.object(telegram_bot, "github_request", return_value=pull_data),
                patch.object(
                    telegram_bot, "build_failure_context", return_value="compile failed"
                ) as mock_failure_context,
                patch.object(
                    telegram_bot,
                    "repair_pull_request_branch",
                    return_value=types.SimpleNamespace(
                        files_changed=["App.kt"], diff="diff", output="repaired"
                    ),
                ) as mock_repair,
                patch.object(
                    telegram_bot, "push", return_value="repair-sha"
                ) as mock_push,
                patch.object(
                    telegram_bot, "return_to_base_branch"
                ) as mock_return_to_base_branch,
            ):
                asyncio.run(telegram_bot.fixpr(update, context))
        finally:
            telegram_bot.watch_ci = original_watch_ci

        self.assertEqual(watched_shas, ["initial-sha", "repair-sha"])
        mock_failure_context.assert_called_once()
        mock_repair.assert_called_once()
        mock_push.assert_called_once_with("bugfix/player", "PR #7 CI repair", "fix")
        mock_return_to_base_branch.assert_called_once()
        self.assertEqual(context.user_data["last_execution"].tests, "PASS")

    def test_passing_existing_pr_preserves_previous_execution(self) -> None:
        bot = importlib.import_module("ai_agent.bot.execution")
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        previous = object()
        context = types.SimpleNamespace(
            args=["7"], user_data={"last_execution": previous}
        )

        async def run_inline(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(bot.asyncio, "to_thread", side_effect=run_inline),
            patch.object(bot, "ensure_github_configured"),
            patch.object(
                bot,
                "active_project",
                return_value=types.SimpleNamespace(github_repository="owner/repo"),
            ),
            patch.object(
                bot,
                "github_request",
                return_value={
                    "state": "open",
                    "html_url": "https://example.test/pr/7",
                    "head": {
                        "sha": "passing-sha",
                        "ref": "feature/search",
                        "repo": {"full_name": "owner/repo"},
                    },
                },
            ),
            patch.object(
                bot,
                "watch_ci",
                new=AsyncMock(return_value=types.SimpleNamespace(state="passed")),
            ),
            patch.object(bot, "repair_pull_request_branch") as repair,
            patch.object(bot, "return_to_base_branch") as restore,
        ):
            asyncio.run(bot.fixpr(update, context))

        repair.assert_not_called()
        restore.assert_called_once_with()
        self.assertIs(context.user_data["last_execution"], previous)
        self.assertIn("already passing", update.message.reply_text.await_args.args[0])
        self.assertNotIn("active_execution", context.user_data)
