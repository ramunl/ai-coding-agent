"""Inspect execution output and manage pending requests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.state import (
    active_execution_text,
    get_verbosity,
    last_execution,
    render_task_queue,
    task_queue,
)
from ai_agent.bot.transport import reply_chunks, require_authorized
from ai_agent.config import COMMAND_TIMEOUT_SECONDS, MAX_LOG_LINES
from ai_agent.plan_state import Verbosity, parse_verbosity
from ai_agent.shell import run
from ai_agent_common import choice_keyboard


def extract_file_diff(diff_text: str, file_name: str) -> str:
    """Extract the matching file sections from a captured git diff."""
    chunks = []
    current = []
    in_target = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if in_target and current:
                chunks.append("\n".join(current))
            current = [line]
            in_target = file_name in line
            continue
        if in_target:
            current.append(line)
    if in_target and current:
        chunks.append("\n".join(current))
    return "\n\n".join(chunks)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show queued work or the active repository git status."""
    if not require_authorized(update):
        return
    active_text = active_execution_text(context)
    if active_text or task_queue(context):
        await reply_chunks(update, render_task_queue(context))
        return
    result = await asyncio.to_thread(run, ["git", "status"])
    await reply_chunks(update, f"Status:\n{result.output}")


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the running task and pending FIFO queue."""
    if not require_authorized(update):
        return
    await reply_chunks(update, render_task_queue(context))


async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show captured debug output or a bounded service log tail."""
    if not require_authorized(update):
        return

    execution = last_execution(context)
    if execution:
        if get_verbosity(context) == Verbosity.DEBUG:
            await reply_chunks(
                update, f"Logs:\n{execution.logs or '(no logs captured)'}"
            )
        else:
            await reply_chunks(
                update,
                "Logs are available in debug mode. Use /verbosity debug, then /logs.",
            )
        return

    requested_lines = (
        int(context.args[0]) if context.args and context.args[0].isdigit() else 60
    )
    lines = max(1, min(requested_lines, MAX_LOG_LINES))
    result = await asyncio.to_thread(
        run,
        ["journalctl", "-u", "ai-coding-agent.service", "-n", str(lines), "--no-pager"],
        Path("/"),
        COMMAND_TIMEOUT_SECONDS,
    )
    await reply_chunks(update, f"Logs:\n{result.output}")


async def verbosity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or change the amount of implementation output in replies."""
    if not require_authorized(update):
        return
    if not context.args:
        selected = get_verbosity(context).value
        await update.message.reply_text(
            f"Verbosity: {selected}\n\nTap to choose:",
            reply_markup=choice_keyboard(
                "verbosity", [level.value for level in Verbosity], active=selected
            ),
        )
        return
    selected = parse_verbosity(context.args[0])
    if not selected:
        await reply_chunks(update, "Usage: /verbosity concise|normal|debug")
        return
    context.user_data["verbosity"] = selected.value
    await reply_chunks(update, f"Verbosity set to {selected.value}.")


async def diff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the last implementation diff summary."""
    if not require_authorized(update):
        return
    execution = last_execution(context)
    if not execution:
        await reply_chunks(update, "No implementation diff is available yet.")
        return
    await reply_chunks(update, execution.diff_summary)


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a captured diff selected by file name or number."""
    if not require_authorized(update):
        return
    execution = last_execution(context)
    if not execution:
        await reply_chunks(update, "No implementation diff is available yet.")
        return
    if not context.args:
        choices = [str(index) for index in range(1, len(execution.files_changed) + 1)]
        await update.message.reply_text(
            "Tap a file number to show its diff:\n"
            + "\n".join(
                f"{index}. {name}"
                for index, name in enumerate(execution.files_changed, 1)
            ),
            reply_markup=choice_keyboard("show", choices),
        )
        return

    selector = context.args[0]
    file_name = ""
    if selector.isdigit():
        index = int(selector) - 1
        if index < 0 or index >= len(execution.files_changed):
            await reply_chunks(
                update, "File number is out of range. Use /diff to list files."
            )
            return
        file_name = execution.files_changed[index]
    else:
        file_name = " ".join(context.args).strip()

    file_diff = extract_file_diff(execution.full_diff, file_name)
    if not file_diff:
        await reply_chunks(update, f"No diff captured for {file_name}.")
        return
    await reply_chunks(update, file_diff)


async def pr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the last implementation pull request URL."""
    if not require_authorized(update):
        return
    execution = last_execution(context)
    if not execution or not execution.pr_url:
        await reply_chunks(update, "No PR is available yet.")
        return
    await reply_chunks(update, execution.pr_url)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Discard pending input or remove a task that has not started."""
    if not require_authorized(update):
        return
    if context.args and context.args[0].isdigit():
        task_id = int(context.args[0])
        queue = task_queue(context)
        for index, task in enumerate(queue):
            if int(task.get("id", 0)) == task_id:
                removed = queue.pop(index)
                await reply_chunks(
                    update,
                    f"Queued task #{task_id} removed.\n\nBranch:\n{removed['branch_name']}",
                )
                return
        await reply_chunks(
            update, f"No queued task #{task_id}. Running tasks cannot be cancelled."
        )
        return

    context.user_data.pop("argument_prompt", None)
    context.user_data.pop("pending_implementation", None)
    context.user_data.pop("pending_plan", None)
    context.user_data.pop("pending_bugfix_clarification", None)
    await reply_chunks(
        update,
        "Pending request discarded. Use /cancel <task-id> to remove a queued task.",
    )


async def _on_verbosity_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, value: str
) -> None:
    if not require_authorized(update):
        return
    selected = parse_verbosity(value)
    if not selected:
        await update.callback_query.edit_message_text("Unknown verbosity choice.")
        return
    context.user_data["verbosity"] = selected.value
    await update.callback_query.edit_message_text(f"Verbosity set to {selected.value}.")


async def _on_show_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, selector: str
) -> None:
    if not require_authorized(update):
        return
    execution = last_execution(context)
    if not execution or not selector.isdigit():
        await update.callback_query.edit_message_text(
            "That diff is no longer available."
        )
        return
    index = int(selector) - 1
    if index < 0 or index >= len(execution.files_changed):
        await update.callback_query.edit_message_text("File number is out of range.")
        return
    file_name = execution.files_changed[index]
    await update.callback_query.edit_message_text(
        extract_file_diff(execution.full_diff, file_name)
        or f"No diff captured for {file_name}."
    )
