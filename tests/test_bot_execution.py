"""Behavior tests for bot execution."""

import asyncio
import importlib
import types
from unittest.mock import AsyncMock, patch

from ai_agent.plan_state import capture_repository_identity
from tests.bot_fixtures import TelegramTestCase


class ExecutionTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        bot = importlib.import_module("ai_agent.bot.execution")
        patcher = patch.object(bot, "cleanup_recovery", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cleanup_failure_does_not_replace_provider_error(self):
        bot = importlib.import_module("ai_agent.bot.execution")
        update = types.SimpleNamespace(
            message=types.SimpleNamespace(reply_text=AsyncMock())
        )

        async def scenario():
            try:
                raise RuntimeError("provider failed")
            finally:
                await bot.reset_to_base_branch(update)

        with (
            patch.object(bot, "cleanup_recovery", side_effect=RuntimeError("secret")),
            patch.object(bot, "return_to_base_branch") as restore,
            patch.object(
                bot, "recovery_details", return_value="refs/ai-agent-recovery/test"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                asyncio.run(scenario())
        restore.assert_not_called()
        replies = str(update.message.reply_text.call_args_list)
        self.assertIn("Cleanup failed", replies)
        self.assertIn("refs/ai-agent-recovery/test", replies)
        self.assertNotIn("secret", replies)

    def test_recovery_report_delivery_cannot_mask_provider_error(self):
        bot = importlib.import_module("ai_agent.bot.execution")

        async def scenario():
            try:
                raise RuntimeError("provider failed")
            finally:
                await bot.reset_to_base_branch(object())

        with (
            patch.object(bot, "cleanup_recovery", side_effect=RuntimeError("cleanup")),
            patch.object(
                bot, "reply_chunks", AsyncMock(side_effect=RuntimeError("network"))
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                asyncio.run(scenario())

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
                    "repository_identity": capture_repository_identity(),
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
                    "repository_identity": capture_repository_identity(),
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
                        "repository_identity": capture_repository_identity(),
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
                    "repository_identity": capture_repository_identity(),
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
                    "repository_identity": capture_repository_identity(),
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
                    "repository_identity": capture_repository_identity(),
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
            {
                "id": 4,
                "branch_name": "feature/a",
                "repository_identity": capture_repository_identity(),
            },
            {
                "id": 5,
                "branch_name": "feature/b",
                "repository_identity": capture_repository_identity(),
            },
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


class RepositoryBindingTests(TelegramTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.planning = importlib.import_module("ai_agent.bot.planning")
        self.execution = importlib.import_module("ai_agent.bot.execution")
        self.update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        self.context = types.SimpleNamespace(user_data={}, args=["Add search"])
        self.project = types.SimpleNamespace(
            name="A", repo_path="/tmp/A", github_repository="owner/A"
        )
        self.project_patch = patch(
            "ai_agent.plan_state.active_project", side_effect=lambda: self.project
        )
        self.project_patch.start()
        self.addCleanup(self.project_patch.stop)

    async def make_plan(self):
        with patch.object(self.planning, "plan_feature", return_value="Add search"):
            await self.planning.plan(self.update, self.context)

    def test_approve_in_a_switch_to_b_confirm_retains_work(self):
        async def scenario():
            await self.make_plan()
            await self.planning.approve(self.update, self.context)
            pending = self.context.user_data["pending_implementation"]
            self.project = types.SimpleNamespace(
                name="B", repo_path="/tmp/B", github_repository="owner/B"
            )
            with patch.object(self.execution, "run_task_queue", AsyncMock()) as run:
                await self.execution.confirm(self.update, self.context)
                run.assert_not_awaited()
            self.assertIs(self.context.user_data["pending_implementation"], pending)
            self.assertTrue(self.context.user_data["pending_plan"].approved)
            self.assertIn("/use A", self.update.message.reply_text.call_args.args[0])

        asyncio.run(scenario())

    def test_revision_and_approval_reject_changed_registration(self):
        async def scenario():
            await self.make_plan()
            original = self.context.user_data["pending_plan"]
            for field, value in (
                ("repo_path", "/tmp/moved"),
                ("github_repository", "other/A"),
            ):
                old = getattr(self.project, field)
                setattr(self.project, field, value)
                with patch.object(self.planning, "revise_feature_plan") as revise:
                    await self.planning.discuss(self.update, self.context)
                    await self.planning.approve(self.update, self.context)
                    revise.assert_not_called()
                self.assertIs(self.context.user_data["pending_plan"], original)
                self.assertFalse(original.approved)
                setattr(self.project, field, old)

        asyncio.run(scenario())

    def test_restored_queue_rejects_legacy_and_mismatched_work_without_cleanup(self):
        async def scenario():
            for identity in (
                None,
                {
                    "project_name": "B",
                    "repo_path": "/tmp/B",
                    "github_repository": "owner/B",
                },
            ):
                task = {
                    "id": 1,
                    "branch_name": "feature/search",
                    "repository_identity": identity,
                }
                self.context.user_data = {"task_queue": [task]}
                with (
                    patch.object(
                        self.execution, "_publish_queued_implementation", AsyncMock()
                    ) as publish,
                    patch.object(
                        self.execution, "reset_to_base_branch", AsyncMock()
                    ) as cleanup,
                ):
                    await self.execution.confirm(self.update, self.context)
                    await self.execution.run_queued_implementation(
                        self.update, self.context, task
                    )
                    publish.assert_not_awaited()
                    cleanup.assert_not_awaited()
                self.assertEqual(self.context.user_data["task_queue"], [task])
                self.assertNotIn("queue_runner_active", self.context.user_data)

        asyncio.run(scenario())

    def test_planning_blocks_switch_before_first_reply_and_clears_on_failure(self):
        actions = importlib.import_module("ai_agent.actions")

        async def scenario(error):
            async def reply(*args):
                with self.assertRaises(actions.ActionError):
                    await actions.use_project("B", self.context.user_data)
                raise error

            with patch.object(self.planning, "reply_chunks", side_effect=reply):
                with self.assertRaises(type(error)):
                    await self.planning.plan(self.update, self.context)
            self.assertNotIn("planning_active", self.context.user_data)

        for error in (RuntimeError("reply failed"), asyncio.CancelledError()):
            asyncio.run(scenario(error))

    def test_queue_runner_blocks_switch_even_when_queue_is_empty(self):
        actions = importlib.import_module("ai_agent.actions")
        self.context.user_data["queue_runner_active"] = True
        with self.assertRaises(actions.ActionError):
            asyncio.run(actions.use_project("B", self.context.user_data))

    def test_bugfix_clarification_keeps_identity_and_rejects_other_project(self):
        async def scenario():
            with patch.object(
                self.planning, "get_bugfix_questions", return_value="What fails?"
            ):
                await self.planning.bugfix_cmd(self.update, self.context)
            pending = self.context.user_data["pending_bugfix_clarification"]
            self.assertEqual(pending["repository_identity"]["project_name"], "A")
            self.project.name = "B"
            with patch.object(self.planning, "get_bugfix_questions") as assess:
                await self.planning.answer(self.update, self.context)
                assess.assert_not_called()
            self.assertIs(
                self.context.user_data["pending_bugfix_clarification"], pending
            )
            self.project.name = "A"
            with (
                patch.object(self.planning, "get_bugfix_questions", return_value=None),
                patch.object(self.planning, "build_bugfix_prompt", return_value="Fix"),
            ):
                await self.planning.answer(self.update, self.context)
            self.assertEqual(
                self.context.user_data["pending_implementation"]["repository_identity"],
                pending["repository_identity"],
            )

        asyncio.run(scenario())

    def test_all_planning_flows_guard_provider_calls_and_release_on_errors(self):
        actions = importlib.import_module("ai_agent.actions")

        async def scenario():
            await self.make_plan()
            identity = self.context.user_data["pending_plan"].repository_identity
            self.context.user_data["pending_bugfix_clarification"] = {
                "bug": "Broken",
                "questions": "Details?",
                "repository_identity": identity,
            }
            for handler in (
                self.planning.plan,
                self.planning.discuss,
                self.planning.implement_cmd,
                self.planning.bugfix_cmd,
                self.planning.answer,
            ):

                async def fail(*args):
                    self.assertTrue(self.context.user_data["planning_active"])
                    with self.assertRaises(actions.ActionError):
                        await actions.use_project("B", self.context.user_data)
                    raise RuntimeError("planner failed")

                with patch.object(self.planning, "think", side_effect=fail):
                    with self.assertRaisesRegex(RuntimeError, "planner failed"):
                        await handler(self.update, self.context)
                self.assertNotIn("planning_active", self.context.user_data)

        asyncio.run(scenario())

    def test_legacy_plan_cannot_be_revised_approved_or_confirmed(self):
        plans = importlib.import_module("ai_agent.plan_state")
        legacy = plans.new_plan_state("legacy", "text")
        self.context.user_data["pending_plan"] = legacy

        async def scenario():
            with (
                patch.object(self.planning, "revise_feature_plan") as revise,
                patch.object(self.execution, "run_task_queue", AsyncMock()) as run,
            ):
                await self.planning.discuss(self.update, self.context)
                await self.planning.approve(self.update, self.context)
                self.assertFalse(legacy.approved)
                legacy.approved = True  # an approval loaded from an older release
                await self.execution.confirm(self.update, self.context)
                revise.assert_not_called()
                run.assert_not_awaited()
            self.assertIs(self.context.user_data["pending_plan"], legacy)
            self.assertIn("Recreate", self.update.message.reply_text.call_args.args[0])

        asyncio.run(scenario())

    def test_cleanup_rejects_registration_changed_during_execution(self):
        async def scenario():
            await self.make_plan()
            await self.planning.approve(self.update, self.context)

            async def changed(*args):
                self.project.repo_path = "/tmp/moved"
                raise RuntimeError("registration changed")

            with (
                patch.object(
                    self.execution,
                    "_publish_queued_implementation",
                    side_effect=changed,
                ),
                patch.object(
                    self.execution, "reset_to_base_branch", AsyncMock()
                ) as reset,
            ):
                with self.assertRaisesRegex(RuntimeError, "registration changed"):
                    await self.execution.confirm(self.update, self.context)
                reset.assert_not_awaited()
            self.assertEqual(len(self.context.user_data["task_queue"]), 1)

        asyncio.run(scenario())
