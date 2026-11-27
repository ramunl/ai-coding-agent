"""Owner-only /fixmerge: validate, resolve and publish an existing PR merge fix."""

from __future__ import annotations

import asyncio
from copy import copy
from dataclasses import asdict

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.actions import busy_reason
from ai_agent.bot.state import think
from ai_agent.bot.transport import (
    prompt_for_arguments,
    reply_chunks,
    require_authorized,
)
from ai_agent.config import redact_sensitive
from ai_agent.merge_conflict import (
    MergeConflictTarget,
    validate_merge_conflict_pr,
)
from ai_agent.merge_conflict_flow import fix_pinned_conflicts
from ai_agent.projects import active_project


def _pinned_message(target: MergeConflictTarget) -> str:
    """Describe the pinned base/head refs and SHAs the fix will use."""
    return (
        f"Pinned merge-conflict target:\n\n"
        f"PR #{target.number}: {target.title}\n"
        f"{target.url}\n\n"
        f"Base: {target.base_ref} ({target.base_sha})\n"
        f"Head: {target.head_ref} ({target.head_sha})\n"
        f"Merge commit: {target.merge_commit_sha or '(not yet available)'}"
    )


async def fix_merge_conflicts(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Validate a PR URL, resolve conflicts in isolation and report publication."""
    if not require_authorized(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await prompt_for_arguments(
            update,
            context,
            "fixmerge",
            "Send the pull request URL to fix (an open PR in the active project).",
        )
        return

    reason = busy_reason(context.user_data)
    if reason:
        await reply_chunks(update, f"Cannot fix merge conflicts while {reason}.")
        return
    if (
        context.user_data.get("pending_plan")
        or context.user_data.get("pending_implementation")
        or context.user_data.get("pending_bugfix_clarification")
    ):
        await reply_chunks(update, "Finish or cancel pending work first.")
        return

    # Set the busy marker before the first await to exclude duplicate requests
    # and project switches, including during remote PR validation.
    data = context.user_data
    data["merge_conflict_active"] = True
    data["planning_active"] = data.get("planning_active", 0) + 1
    try:
        project = active_project()
        target = await asyncio.to_thread(validate_merge_conflict_pr, text)
        data["pending_merge_conflict"] = {**asdict(target), "status": "running"}
        application = getattr(context, "application", None)
        if application is not None:
            application.mark_data_for_update_persistence(
                user_ids=update.effective_chat.id
            )
        await reply_chunks(update, _pinned_message(target) + "\n\nResolving conflicts…")
        work = asyncio.create_task(
            think(
                context,
                "fixmerge",
                target.url,
                fix_pinned_conflicts,
                project,
                target,
                data.get("implementation_agent"),
            )
        )
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            # A thread cannot be cancelled: retain the busy guard and checkout
            # until its provider and publication have finished.
            await work
            raise
        data["pending_merge_conflict"].update(status="done", message=result)
        await reply_chunks(update, result)
    except Exception as error:
        message = redact_sensitive(str(error))[:2000]
        if data.get("pending_merge_conflict", {}).get("status") == "running":
            data["pending_merge_conflict"].update(status="failed", message=message)
        await reply_chunks(update, f"Merge conflict fix failed: {message}")
    finally:
        data.pop("merge_conflict_active", None)
        data["planning_active"] -= 1
        if not data["planning_active"]:
            data.pop("planning_active", None)


async def _on_fixmerge_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, selection: str
) -> None:
    """Handle a Fix Merge Conflicts button tap by prompting for a PR URL."""
    if not require_authorized(update):
        return
    invocation = copy(context)
    invocation.args = []
    message_update = Update(update.update_id, message=update.callback_query.message)
    await fix_merge_conflicts(message_update, invocation)
