"""Behavior tests for bot execution."""

import asyncio
import importlib
import types
from unittest.mock import AsyncMock, patch

from tests.bot_fixtures import TelegramTestCase


class ExecutionTests(TelegramTestCase):
    def test_confirm_queues_pending_work_when_runner_is_active(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.execution")

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
        self.assertEqual(
            context.user_data["task_queue"][0]["branch_name"], "bugfix/fix-build"
        )
        self.assertNotIn("pending_implementation", context.user_data)

    def test_confirm_repairs_failed_ci_and_polls_repair_commit(self) -> None:
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
                patch("ai_agent.bot.ci_monitor.CI_FIX_ATTEMPTS", 1),
                patch.object(
                    telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread
                ),
                patch.object(telegram_bot, "ensure_github_configured"),
                patch.object(
                    telegram_bot,
                    "implement",
                    return_value=types.SimpleNamespace(
                        files_changed=["App.kt"], diff="diff1", output="implemented"
                    ),
                ),
                patch.object(
                    telegram_bot, "push", side_effect=["initial-sha", "repair-sha"]
                ) as mock_push,
                patch.object(
                    telegram_bot,
                    "create_pull_request",
                    return_value=types.SimpleNamespace(
                        number=42,
                        url="https://example.test/pr",
                        head_sha="stale-or-pr-sha",
                    ),
                ),
                patch.object(
                    telegram_bot, "build_failure_context", return_value="compile failed"
                ) as mock_failure_context,
                patch.object(
                    telegram_bot,
                    "repair_implementation",
                    return_value=types.SimpleNamespace(
                        files_changed=["App.kt"], diff="diff2", output="repaired"
                    ),
                ) as mock_repair,
                patch.object(
                    telegram_bot, "return_to_base_branch"
                ) as mock_return_to_base_branch,
            ):
                asyncio.run(telegram_bot.confirm(update, context))
        finally:
            telegram_bot.watch_ci = original_watch_ci

        self.assertEqual(watched_shas, ["initial-sha", "repair-sha"])
        self.assertEqual(mock_push.call_count, 2)
        mock_failure_context.assert_called_once()
        mock_repair.assert_called_once()
        mock_return_to_base_branch.assert_called_once()
        self.assertNotIn("pending_implementation", context.user_data)
        self.assertEqual(context.user_data["last_execution"].tests, "PASS")
        joined_replies = "\n\n".join(message.replies)
        self.assertIn("CI passed", joined_replies)
        self.assertIn("Implementation completed.", joined_replies)

    def test_confirm_drains_existing_queue_before_new_task_fifo(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.execution")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        async def fake_watch_ci(_update, head_sha):
            watched_shas.append(head_sha)
            return types.SimpleNamespace(
                state="passed",
                summary="CI passed",
                url=f"https://example.test/{head_sha}",
            )

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        def fake_implement(_prompt, branch_name, _agent):
            implemented_branches.append(branch_name)
            return types.SimpleNamespace(
                files_changed=[f"{branch_name}.kt"],
                diff="diff",
                output=f"implemented {branch_name}",
            )

        implemented_branches = []
        watched_shas = []
        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
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
                patch.object(
                    telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread
                ),
                patch.object(telegram_bot, "ensure_github_configured"),
                patch.object(telegram_bot, "implement", side_effect=fake_implement),
                patch.object(
                    telegram_bot, "push", side_effect=["first-sha", "second-sha"]
                ),
                patch.object(
                    telegram_bot,
                    "create_pull_request",
                    side_effect=[
                        types.SimpleNamespace(
                            number=1,
                            url="https://example.test/pr/1",
                            head_sha="first-sha",
                        ),
                        types.SimpleNamespace(
                            number=2,
                            url="https://example.test/pr/2",
                            head_sha="second-sha",
                        ),
                    ],
                ),
                patch.object(telegram_bot, "return_to_base_branch"),
            ):
                asyncio.run(telegram_bot.confirm(update, context))
        finally:
            telegram_bot.watch_ci = original_watch_ci

        self.assertEqual(implemented_branches, ["feature/first", "feature/second"])
        self.assertEqual(watched_shas, ["first-sha", "second-sha"])
        self.assertEqual(context.user_data["task_queue"], [])
        self.assertNotIn("queue_runner_active", context.user_data)
        self.assertIn("Queued task #2 at position 2", "\n".join(message.replies))

    def test_confirm_reports_failed_ci_after_repair_attempts_are_exhausted(
        self,
    ) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.execution")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        async def fake_watch_ci(_update, head_sha):
            watched_shas.append(head_sha)
            return types.SimpleNamespace(
                state="failed",
                summary="CI failed",
                url=f"https://example.test/{head_sha}",
            )

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        watched_shas = []
        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
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
                patch("ai_agent.bot.ci_monitor.CI_FIX_ATTEMPTS", 1),
                patch.object(
                    telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread
                ),
                patch.object(telegram_bot, "ensure_github_configured"),
                patch.object(
                    telegram_bot,
                    "implement",
                    return_value=types.SimpleNamespace(
                        files_changed=["App.kt"], diff="diff1", output="implemented"
                    ),
                ),
                patch.object(
                    telegram_bot, "push", side_effect=["initial-sha", "repair-sha"]
                ) as mock_push,
                patch.object(
                    telegram_bot,
                    "create_pull_request",
                    return_value=types.SimpleNamespace(
                        number=42,
                        url="https://example.test/pr",
                        head_sha="stale-or-pr-sha",
                    ),
                ),
                patch.object(
                    telegram_bot, "build_failure_context", return_value="compile failed"
                ),
                patch.object(
                    telegram_bot,
                    "repair_implementation",
                    return_value=types.SimpleNamespace(
                        files_changed=["App.kt"], diff="diff2", output="repaired"
                    ),
                ),
                patch.object(telegram_bot, "return_to_base_branch"),
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

    def test_implementation_failure_releases_runner_and_restores_base(self) -> None:
        bot = importlib.import_module("ai_agent.bot.execution")
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        context = types.SimpleNamespace(
            user_data={
                "pending_implementation": {
                    "change": "Add search",
                    "codex_prompt": "Implement search",
                    "branch_name": "feature/search",
                }
            }
        )

        async def run_inline(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(bot.asyncio, "to_thread", side_effect=run_inline),
            patch.object(bot, "ensure_github_configured"),
            patch.object(bot, "implement", side_effect=RuntimeError("provider failed")),
            patch.object(bot, "push") as push,
            patch.object(bot, "return_to_base_branch") as restore,
        ):
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                asyncio.run(bot.confirm(update, context))

        restore.assert_called_once_with()
        push.assert_not_called()
        self.assertNotIn("active_execution", context.user_data)
        self.assertNotIn("queue_runner_active", context.user_data)
        self.assertEqual(context.user_data["task_queue"], [])


class ResumeRestoredQueueTests(TelegramTestCase):
    """After a restart the queue is restored but no runner is active."""

    def _run_confirm(self, user_data: dict):
        execution = importlib.import_module("ai_agent.bot.execution")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(user_data=user_data)
        with patch.object(
            execution, "run_queued_implementation", AsyncMock()
        ) as run_one:
            asyncio.run(execution.confirm(update, context))
        return message.replies, run_one, context

    def test_confirm_resumes_restored_queue_when_nothing_is_pending(self) -> None:
        tasks = [
            {"id": 4, "branch_name": "feature/a"},
            {"id": 5, "branch_name": "feature/b"},
        ]
        replies, run_one, context = self._run_confirm({"task_queue": list(tasks)})

        self.assertIn("Resuming 2 queued task(s).", replies[0])
        self.assertEqual(
            [call.args[2]["id"] for call in run_one.await_args_list], [4, 5]
        )
        self.assertEqual(context.user_data["task_queue"], [])
        self.assertNotIn("queue_runner_active", context.user_data)

    def test_confirm_does_not_start_a_second_runner(self) -> None:
        replies, run_one, _ = self._run_confirm(
            {
                "task_queue": [{"id": 1, "branch_name": "feature/a"}],
                "queue_runner_active": True,
            }
        )
        run_one.assert_not_awaited()
        self.assertIn("No pending implementation", replies[0])

    def test_confirm_with_empty_queue_keeps_existing_message(self) -> None:
        replies, run_one, _ = self._run_confirm({})
        run_one.assert_not_awaited()
        self.assertIn("No pending implementation", replies[0])
