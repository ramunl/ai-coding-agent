"""Task requests from the dashboard: make a task from a todo, start, remove.

Planning a task runs the same /plan handler as the chat (through the owner
chat stand-in in work_actions), in the task's repository. The agent plans one
thing at a time, so a task that cannot start yet waits as "todo" with the
reason, and is started later from the Tasks window.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from telegram.ext import CallbackContext

from ai_agent import log_scan
from ai_agent.actions import ActionError, busy_reason, use_project
from ai_agent.bot import work_actions
from ai_agent.bot.planning import plan
from ai_agent.config import redact_sensitive
from ai_agent.plan_state import parse_plan_document
from ai_agent.projects import ProjectError, active_project, get_project
from ai_agent.task_split import SplitError, split_pending
from ai_agent.tasks import (
    add_task,
    can_retry,
    find_task,
    remove_task,
    set_stage,
    waiting_for,
)

logger = logging.getLogger(__name__)

TASK_ACTIONS = (
    "create_task",
    "start_task",
    "remove_task",
    "accept_task",
    "dismiss_task",
    "split_plan",
    "retry_task",
    "create_autopilot_task",
    "set_autopilot",
)
REMOVABLE = ("todo", "stopped", "pr", "done", "blocked", "ops_required")


def start_refusal(data: Any) -> str | None:
    """Why no task can start planning now, or None."""
    if work_actions._starting or data.get("planning_task"):
        return "another request is being planned"
    if data.get("pending_plan") or data.get("pending_implementation"):
        return "a plan is waiting for approval or confirmation"
    if data.get("pending_bugfix_clarification"):
        return "a bug report is waiting for answers"
    return None


async def _plan_task(
    update: Any, context: Any, task: dict, planner: str | None = None
) -> None:
    """Run /plan for the task (with planner, if given), then tie the plan to it."""
    data = context.user_data
    before = data.get("pending_plan")
    chosen = data.get("planning_agent")
    if planner:  # this plan only: the owner's setting comes back afterwards
        data["planning_agent"] = planner
    failure = None
    try:
        await plan(update, context)
    except Exception as error:
        failure = work_actions.error_text(error).splitlines()[0][:150]
        raise
    finally:
        if planner and data.get("planning_agent") == planner:
            if chosen is None:
                data.pop("planning_agent", None)
            else:
                data["planning_agent"] = chosen
        data.pop("planning_task", None)
        after = data.get("pending_plan")
        if after is not None and after is not before:
            task["plan_id"] = after.id
            task["branch"] = parse_plan_document(after.plan_text, after.feature).branch
            set_stage(task, "planned")
        elif task.get("stage") == "planning":
            reason = f"planning failed: {failure}" if failure else "no plan was made"
            set_stage(task, "todo", f"{reason}; see the bot chat")


async def _switch_to(ptb_app: Any, data: Any, repo: str) -> None:
    if active_project().name == repo:
        return
    project = await use_project(repo, data)
    try:  # the bot's name shows the active project; cosmetic, rate-limited
        await ptb_app.bot.set_my_name(name=f"Coding AI Agent - {project.name}"[:64])
    except Exception as error:
        logger.info("Could not update bot name (ignored): %s", error)


async def start(
    ptb_app: Any, owner_id: int, task: dict, planner: str | None = None
) -> str:
    """Start planning a task in its repository; raise ActionError if it can't."""
    data = ptb_app.user_data[owner_id]
    reason = start_refusal(data)
    if reason:
        raise ActionError(f"Not started: {reason}.")
    await _switch_to(ptb_app, data, task["repo"])
    update = work_actions.chat_update(ptb_app.bot, owner_id)
    context = CallbackContext(ptb_app, chat_id=owner_id, user_id=owner_id)
    context.args = task.get("text", task["title"]).split()
    data["planning_task"] = task["id"]
    set_stage(task, "planning")

    async def handler(update: Any, context: Any) -> None:
        await _plan_task(update, context, task, planner)

    run = await work_actions._start(handler, update, context)
    if not run.done():
        work_actions._starting.add(run)
    return work_actions.summary(
        update.message, "Planning; the plan will be in the bot chat."
    )


async def retry(ptb_app: Any, owner_id: int, task: dict, tool: str) -> str:
    """Run a task's approved plan again, with tool; raise ActionError if it can't."""
    data = ptb_app.user_data[owner_id]
    if not can_retry(task):
        raise ActionError("This task has no failed run to retry; start planning it.")
    reason = start_refusal(data) or busy_reason(data)
    if reason:
        raise ActionError(f"Not retried: {reason}.")
    await _switch_to(ptb_app, data, task["repo"])
    data["pending_implementation"] = {**task["run"], "implementation_agent": tool}
    task.pop("worker_failed", None)
    set_stage(task, "implementing", f"retrying with {tool}")
    try:
        await work_actions.run_work_action(ptb_app, owner_id, "confirm_work", {})
    except Exception:
        data.pop("pending_implementation", None)
        set_stage(task, "stopped", f"retry with {tool} could not start")
        raise
    return f"Retrying “{task['title'][:80]}” with {tool}; progress is in the bot chat."


async def run_task_action(
    ptb_app: Any, owner_id: int, action: str, args: dict[str, Any]
) -> str:
    """Run one validated task action for the owner; return a one-line result."""
    data = ptb_app.user_data[owner_id]
    if action == "split_plan":
        if work_actions._starting:
            raise ActionError("Wait until the agent finishes planning.")
        try:
            return split_pending(data)
        except SplitError as error:
            raise ActionError(str(error)) from error
    if action in ("create_task", "create_autopilot_task"):
        try:
            get_project(args["repo"])
        except ProjectError as error:
            raise ActionError(str(error)) from error
        todo = None if args["todo"] == "-" else args["todo"]
        task = add_task(data, args["text"], args["repo"], todo, args.get("images"))
        if action == "create_autopilot_task":
            task["autopilot"] = True
        try:
            message = await start(ptb_app, owner_id, task)
        except ActionError as error:
            set_stage(
                task, "todo", str(error).removeprefix("Not started: ").rstrip(".")
            )
        except Exception as error:
            # The saved task owns its images now; inbox failure cleanup must
            # not remove them just because immediate startup failed.
            logger.warning(
                "Task %s startup failed: %s",
                task["id"],
                redact_sensitive(str(error) or type(error).__name__),
            )
            set_stage(task, "todo", "planning could not start; see the bot logs")
        else:
            return f"Task added. {message}"
        return f"Task added as To do: {task['note']}."
    task = find_task(data, args["task"])
    if task is None:
        raise ActionError("That task no longer exists.")
    if action in ("accept_task", "dismiss_task"):
        if task.get("stage") != "proposed":
            raise ActionError("That task is not a proposal any more.")
        if action == "accept_task":
            set_stage(task, "todo")
            return f"Accepted as To do: {task['title']}"
        log_scan.dismiss(data, task, time.time())
        remove_task(data, task)
        return "Dismissed; that error will not be proposed again for 30 days."
    if action == "set_autopilot":
        task["autopilot"] = args["value"] == "on"
        task.pop("autopilot_stuck", None)
        if task["autopilot"]:
            return (
                f"Autopilot on: the worker plans, approves and runs “"
                f"{task['title'][:80]}” to a pull request; you review the PR."
            )
        return f"Autopilot off: “{task['title'][:80]}” waits for your approval."
    if action == "retry_task":
        return await retry(ptb_app, owner_id, task, args["tool"])
    if action == "start_task":
        if task.get("stage") not in ("todo", "stopped", "blocked", "ops_required"):
            raise ActionError(
                "Only a To do, stopped, blocked or Ops-action task can be started."
            )
        previous = waiting_for(task, data)
        if previous is not None:
            raise ActionError(
                f"This part waits for “{previous['title']}” to have a pull request."
            )
        return await start(ptb_app, owner_id, task)
    if task.get("stage") not in REMOVABLE:
        raise ActionError("A task can be removed only when it is not in progress.")
    remove_task(data, task)
    return f"Task removed: {task['title']}"
