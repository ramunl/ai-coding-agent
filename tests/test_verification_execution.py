"""Queue dispatch keeps fixed verification outside paid implementation and PRs."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ai_agent.context_sources import ContextBlockedError
from ai_agent.plan_state import capture_repository_identity
from tests.bot_fixtures import TelegramTestCase


class VerificationExecutionTests(TelegramTestCase):
    def test_verification_bypasses_model_publication_and_requires_cleanup(self):
        bot = importlib.import_module("ai_agent.bot.execution")
        verification = importlib.import_module("ai_agent.verification")
        for restored in (True, False):
            with self.subTest(restored=restored):
                identity = capture_repository_identity()
                task = {
                    "id": "task",
                    "repo": identity["project_name"],
                    "branch": "feature/verify",
                    "stage": "planned",
                }
                item = {
                    "checkpoint_task": "task",
                    "repository_identity": identity,
                    "branch_name": task["branch"],
                    "implementation_agent": "claude",
                    "verification_profile": "ai-dashboard:baseline-v1",
                }
                update = SimpleNamespace(effective_user=SimpleNamespace(id=123))
                context = SimpleNamespace(user_data={"tasks": [task]})
                with (
                    patch.object(bot, "reply_chunks", AsyncMock()),
                    patch.object(bot, "set_active_execution"),
                    patch.object(bot, "clear_progress_state"),
                    patch.object(bot, "bind_checkpoint"),
                    patch.object(
                        bot, "reset_to_base_branch", AsyncMock(return_value=restored)
                    ),
                    patch.object(
                        bot, "_publish_queued_implementation", AsyncMock()
                    ) as publish,
                    patch.object(verification, "run_verification", Mock()) as verify,
                ):
                    # Exercise dispatch and cleanup beneath the separately tested
                    # execution lease and lifecycle journal decorators.
                    invocation = bot.run_queued_implementation.__wrapped__.__wrapped__(
                        update, context, item
                    )
                    if restored:
                        asyncio.run(invocation)
                    else:
                        with self.assertRaisesRegex(ContextBlockedError, "cleanup"):
                            asyncio.run(invocation)
                    verify.assert_called_once_with(item["verification_profile"])
                    publish.assert_not_called()

    def test_failed_verification_reports_checks_instead_of_missing_pr(self):
        bot = importlib.import_module("ai_agent.bot.execution")
        verification = importlib.import_module("ai_agent.verification")
        identity = capture_repository_identity()
        task = {
            "id": "task",
            "repo": identity["project_name"],
            "branch": "feature/verify",
            "stage": "planned",
        }
        item = {
            "checkpoint_task": "task",
            "repository_identity": identity,
            "branch_name": task["branch"],
            "implementation_agent": "claude",
            "verification_profile": "ai-dashboard:baseline-v1",
        }
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123))
        context = SimpleNamespace(user_data={"tasks": [task]}, application=Mock())
        error = verification.NoCodeOutcomeError(
            verification.blocked_report("ruff check failed (exit 1).")
        )
        with (
            patch.object(bot, "reply_chunks", AsyncMock()) as reply,
            patch.object(bot, "set_active_execution"),
            patch.object(bot, "clear_progress_state"),
            patch.object(bot, "bind_checkpoint"),
            patch.object(bot, "checkpoint"),
            patch.object(bot, "discard_empty_branch"),
            patch.object(bot, "reset_to_base_branch", AsyncMock(return_value=True)),
            patch.object(bot, "_publish_queued_implementation", AsyncMock()) as publish,
            patch.object(verification, "run_verification", side_effect=error),
        ):
            asyncio.run(
                bot.run_queued_implementation.__wrapped__.__wrapped__(
                    update, context, item
                )
            )
            messages = "\n".join(call.args[1] for call in reply.call_args_list)
            self.assertIn("Verification blocked.", messages)
            self.assertIn("ruff check failed", messages)
            self.assertNotIn("no commit or PR was created", messages)
            self.assertEqual(task["stage"], "blocked")
            publish.assert_not_called()
