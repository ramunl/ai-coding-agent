"""Owner-only /fixmerge: pin an open PR for a later merge-conflict fix."""

from __future__ import annotations

import asyncio
from copy import copy
from dataclasses import asdict

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.transport import (
    prompt_for_arguments,
    reply_chunks,
    require_authorized,
)
from ai_agent.merge_conflict import (
    MergeConflictError,
    MergeConflictTarget,
    validate_merge_conflict_pr,
)


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
    """Validate a PR URL and store its pinned base/head refs and SHAs."""
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

    try:
        target = await asyncio.to_thread(validate_merge_conflict_pr, text)
    except (MergeConflictError, RuntimeError) as error:
        await reply_chunks(update, str(error))
        return

    context.user_data["pending_merge_conflict"] = asdict(target)
    await reply_chunks(update, _pinned_message(target))


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
