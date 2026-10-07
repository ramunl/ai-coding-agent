"""Work requests from the dashboard: start, revise, approve, confirm, cancel.

Each one runs the same handler as its Telegram command (/plan, /implement,
/bugfix, /answer, /discuss, /approve, /confirm, /cancel), through a stand-in
for the owner's chat. So the rules stay in one place, and replies and task
progress still arrive in the bot chat; the dashboard only gets a one-line
result.

A request is checked against the current state first, so a stale tap (the
plan is already gone, the task already started) is refused with a clear
message instead of running the handler for nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import MutableMapping
from types import SimpleNamespace
from typing import Any

from telegram.ext import CallbackContext

from ai_agent.actions import ActionError
from ai_agent.bot.execution import confirm
from ai_agent.bot.inspection import cancel
from ai_agent.bot.planning import (
    answer,
    approve,
    bugfix_cmd,
    discuss,
    implement_cmd,
    plan,
)

logger = logging.getLogger(__name__)

FIRST_REPLY_SECONDS = 15
SUMMARY_LENGTH = 200
WORK_ACTIONS = (
    "approve_plan",
    "confirm_work",
    "cancel_pending",
    "remove_queued",
    "start_work",
    "discuss_plan",
    "answer_bugfix",
)
STARTERS = {"plan": plan, "implement": implement_cmd, "bugfix": bugfix_cmd}

# Long handlers started here; kept so they are not garbage-collected mid-run.
_runs: set[asyncio.Task] = set()
# Planning requests still thinking; a second one would overwrite the first.
_starting: set[asyncio.Task] = set()


class ChatMessage:
    """Stands in for the owner's message: replies go to the owner's chat."""

    text = ""
    reply_to_message = None

    def __init__(self, bot: Any, chat_id: int) -> None:
        """Remember where replies go; nothing is sent yet."""
        self._bot = bot
        self._chat_id = chat_id
        self.sent: list[str] = []
        self.replied = asyncio.Event()

    async def reply_text(self, text: str, **options: Any) -> Any:
        """Send text to the owner's chat, as a reply to a command would."""
        self.sent.append(text)
        self.replied.set()
        return await self._bot.send_message(chat_id=self._chat_id, text=text, **options)


def chat_update(bot: Any, chat_id: int) -> SimpleNamespace:
    """What handlers read from an update, for a request with no message."""
    return SimpleNamespace(
        update_id=0,
        message=ChatMessage(bot, chat_id),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=chat_id),
        callback_query=None,
    )


def refusal(action: str, args: dict[str, str], user_data: MutableMapping) -> str | None:
    """Why the request makes no sense in the current state, or None."""
    pending_plan = user_data.get("pending_plan")
    pending = user_data.get("pending_implementation")
    queue = user_data.get("task_queue") or []
    if action == "start_work":
        if _starting:
            return "A request is already being planned; wait for the bot chat."
        if pending_plan or pending or user_data.get("pending_bugfix_clarification"):
            return "Finish or cancel the pending work first."
        return None
    if action == "answer_bugfix":
        if not user_data.get("pending_bugfix_clarification"):
            return "There are no bugfix questions to answer."
        return "The last answer is still being checked." if _starting else None
    if action == "discuss_plan":
        if not pending_plan:
            return "There is no plan to revise."
        return "The plan is already being revised." if _starting else None
    if _starting and action in ("approve_plan", "confirm_work"):
        # The plan on screen is about to be replaced by the one being written.
        return "A plan is still being written; wait for it in the bot chat."
    if action == "approve_plan":
        if not pending_plan:
            return "There is no plan to approve."
        return "The plan is already approved." if pending_plan.approved else None
    if action == "confirm_work":
        if pending_plan and not pending_plan.approved and not pending:
            return "The plan is not approved yet."
        is_resumable = queue and not user_data.get("queue_runner_active")
        if (
            not pending
            and not (pending_plan and pending_plan.approved)
            and not is_resumable
        ):
            return "There is nothing to confirm."
        return None
    if action == "remove_queued":
        if not any(str(task.get("id")) == args["task"] for task in queue):
            return f"Task #{args['task']} is not in the queue (it may have started)."
        return None
    has_pending = (
        pending_plan or pending or user_data.get("pending_bugfix_clarification")
    )
    return None if has_pending else "There is nothing pending to cancel."


def summary(message: ChatMessage, fallback: str) -> str:
    """First reply as one short line, for the dashboard's result."""
    if not message.sent:
        return fallback
    return " ".join(message.sent[0].split())[:SUMMARY_LENGTH]


def _finished(task: asyncio.Task) -> None:
    _runs.discard(task)
    _starting.discard(task)
    if not task.cancelled() and task.exception():
        logger.error(
            "Work started from the dashboard failed", exc_info=task.exception()
        )


async def _start(handler: Any, update: SimpleNamespace, context: Any) -> asyncio.Task:
    """Run a long handler in the background; return once it has first replied."""
    run = asyncio.create_task(handler(update, context))
    _runs.add(run)
    run.add_done_callback(_finished)
    replied = asyncio.create_task(update.message.replied.wait())
    await asyncio.wait(
        {run, replied},
        timeout=FIRST_REPLY_SECONDS,
        return_when=asyncio.FIRST_COMPLETED,
    )
    replied.cancel()
    return run


async def run_work_action(
    ptb_app: Any, owner_id: int, action: str, args: dict[str, str]
) -> str:
    """Run one validated work action for the owner; return a one-line result."""
    reason = refusal(action, args, ptb_app.user_data[owner_id])
    if reason:
        raise ActionError(reason)
    update = chat_update(ptb_app.bot, owner_id)
    context = CallbackContext(ptb_app, chat_id=owner_id, user_id=owner_id)
    if action == "confirm_work":
        # /confirm also runs the queue, which can take an hour: start it and
        # answer as soon as it has said what it queued.
        context.args = []
        await _start(confirm, update, context)
        return summary(update.message, "Started; progress is in the bot chat.")
    if action == "start_work":
        # Planning takes minutes; the plan itself arrives in the bot chat.
        context.args = args["text"].split()
        run = await _start(STARTERS[args["kind"]], update, context)
        if not run.done():
            _starting.add(run)
        return summary(update.message, "Started; the result will be in the bot chat.")
    if action == "answer_bugfix":
        # The agent re-checks the report; more questions or the fix plan follow.
        context.args = args["text"].split()
        run = await _start(answer, update, context)
        if not run.done():
            _starting.add(run)
        return summary(update.message, "Checking; the result will be in the bot chat.")
    if action == "discuss_plan":
        # Revising takes minutes too; the new revision arrives in the bot chat.
        context.args = args["text"].split()
        run = await _start(discuss, update, context)
        if not run.done():
            _starting.add(run)
        return summary(update.message, "Revising; the plan will be in the bot chat.")
    if action == "approve_plan":
        context.args = []
        await approve(update, context)
        return summary(update.message, "Plan approved.")
    context.args = [args["task"]] if action == "remove_queued" else []
    await cancel(update, context)
    return summary(update.message, "Done.")
