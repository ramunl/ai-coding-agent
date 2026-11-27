"""Behavior tests for the owner-only /fixmerge command and button."""

import asyncio
import importlib
import types
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

from ai_agent.merge_conflict import MergeConflictError, MergeConflictTarget
from tests.bot_fixtures import TelegramTestCase


class FakeMessage:
    """Collect reply_text calls like the other bot handler tests."""

    def __init__(self) -> None:
        self.replies = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class MergeConflictsBotTests(TelegramTestCase):
    def test_fixmerge_without_url_prompts_owner(self) -> None:
        bot = importlib.import_module("ai_agent.bot.merge_conflicts")
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=object()
        )
        context = types.SimpleNamespace(args=[], user_data={})

        with patch.object(bot, "prompt_for_arguments", new=AsyncMock()) as prompt:
            asyncio.run(bot.fix_merge_conflicts(update, context))

        prompt.assert_awaited_once()
        self.assertEqual(prompt.await_args.args[2], "fixmerge")

    def test_fixmerge_ignores_non_owner(self) -> None:
        bot = importlib.import_module("ai_agent.bot.merge_conflicts")
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=999), message=object()
        )
        context = types.SimpleNamespace(args=[], user_data={})

        with patch.object(bot, "prompt_for_arguments", new=AsyncMock()) as prompt:
            asyncio.run(bot.fix_merge_conflicts(update, context))

        prompt.assert_not_awaited()
        self.assertNotIn("pending_merge_conflict", context.user_data)

    def test_fixmerge_stores_pinned_target_and_replies_refs(self) -> None:
        bot = importlib.import_module("ai_agent.bot.merge_conflicts")
        target = MergeConflictTarget(
            number=5,
            url="https://github.com/owner/repo/pull/5",
            title="Fix conflicts",
            base_ref="main",
            base_sha="base-sha",
            head_ref="fix/branch",
            head_sha="head-sha",
            merge_commit_sha="merge-sha",
        )
        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(
            args=["https://github.com/owner/repo/pull/5"], user_data={}
        )

        with (
            patch.object(bot, "validate_merge_conflict_pr", return_value=target),
            patch.object(
                bot, "fix_pinned_conflicts", return_value="Updated PR #5"
            ) as fix,
        ):
            asyncio.run(bot.fix_merge_conflicts(update, context))

        self.assertEqual(
            context.user_data["pending_merge_conflict"],
            {**asdict(target), "status": "done", "message": "Updated PR #5"},
        )
        fix.assert_called_once()
        self.assertNotIn("thinking", context.user_data)
        self.assertNotIn("planning_active", context.user_data)
        reply = "\n".join(message.replies)
        for pinned in ("main (base-sha)", "fix/branch (head-sha)", "merge-sha"):
            self.assertIn(pinned, reply)

    def test_fixmerge_rejection_replies_error_and_stores_nothing(self) -> None:
        bot = importlib.import_module("ai_agent.bot.merge_conflicts")
        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(
            args=["https://github.com/owner/repo/pull/5"], user_data={}
        )

        with patch.object(
            bot,
            "validate_merge_conflict_pr",
            side_effect=MergeConflictError("fork"),
        ):
            asyncio.run(bot.fix_merge_conflicts(update, context))

        self.assertIn("fork", message.replies[0])
        self.assertNotIn("pending_merge_conflict", context.user_data)

    def test_fixmerge_button_tap_prompts_with_cleared_args(self) -> None:
        bot = importlib.import_module("ai_agent.bot.merge_conflicts")
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=123), reply_text=AsyncMock()
        )
        update = types.SimpleNamespace(
            update_id=1,
            effective_chat=message.chat,
            callback_query=types.SimpleNamespace(message=message),
        )
        context = types.SimpleNamespace(args=["stale"], user_data={})

        with patch.object(bot, "fix_merge_conflicts", new=AsyncMock()) as handler:
            asyncio.run(bot._on_fixmerge_tap(update, context, ""))

        handler.assert_awaited_once()
        self.assertEqual(handler.await_args.args[1].args, [])
        self.assertEqual(context.args, ["stale"])

    def test_fixmerge_button_tap_ignores_non_owner(self) -> None:
        bot = importlib.import_module("ai_agent.bot.merge_conflicts")
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=999), reply_text=AsyncMock()
        )
        update = types.SimpleNamespace(
            update_id=1,
            effective_chat=message.chat,
            callback_query=types.SimpleNamespace(message=message),
        )
        context = types.SimpleNamespace(args=[], user_data={})

        with patch.object(bot, "fix_merge_conflicts", new=AsyncMock()) as handler:
            asyncio.run(bot._on_fixmerge_tap(update, context, ""))

        handler.assert_not_awaited()

    def test_busy_request_does_not_validate_or_spend_tokens(self):
        bot = importlib.import_module("ai_agent.bot.merge_conflicts")
        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(
            args=["https://github.com/owner/repo/pull/5"],
            user_data={"merge_conflict_active": True},
        )
        with patch.object(bot, "validate_merge_conflict_pr") as validate:
            asyncio.run(bot.fix_merge_conflicts(update, context))
        validate.assert_not_called()
        self.assertIn("running", message.replies[0])

    def test_restarted_fix_is_reported_as_interrupted(self):
        state = importlib.import_module("ai_agent.bot.state")
        result = state.snapshot({"pending_merge_conflict": {"status": "running"}})
        self.assertEqual(result["merge_conflict"]["status"], "interrupted")

    def test_task_fix_keeps_task_id_in_published_result(self):
        bot = importlib.import_module("ai_agent.bot.merge_conflicts")
        state = importlib.import_module("ai_agent.bot.state")
        target = MergeConflictTarget(
            5,
            "https://github.com/owner/repo/pull/5",
            "Fix",
            "main",
            "a" * 40,
            "feature/fix",
            "b" * 40,
            "",
        )
        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=[target.url], user_data={})
        with (
            patch.object(bot, "validate_merge_conflict_pr", return_value=target),
            patch.object(bot, "fix_pinned_conflicts", return_value="Updated PR #5"),
        ):
            asyncio.run(bot.fix_merge_conflicts(update, context, task_id="abcd1234"))
        result = state.snapshot(context)["merge_conflict"]
        self.assertEqual(result["task_id"], "abcd1234")
        self.assertEqual(result["status"], "done")
