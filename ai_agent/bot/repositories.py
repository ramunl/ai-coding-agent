"""Manage projects and working branches."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.state import active_execution_text
from ai_agent.bot.transport import (
    prompt_for_arguments,
    reply_chunks,
    require_authorized,
    send_rich_message,
)
from ai_agent.config import redact_sensitive
from ai_agent.projects import (
    ProjectError,
    active_project,
    add_project,
    clone_url,
    list_projects,
    remove_project,
    set_active,
)
from ai_agent.shell import run
from ai_agent.workflow import validate_branch_name
from ai_agent_common import choice_keyboard

logger = logging.getLogger(__name__)


async def branches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List local and remote branches for the active project."""
    if not require_authorized(update):
        return
    result = await asyncio.to_thread(run, ["git", "branch", "-a"])
    await reply_chunks(update, f"Branches:\n{result.output}")


async def branch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or switch the active branch when no implementation is running."""
    if not require_authorized(update):
        return

    if not context.args:
        result = await asyncio.to_thread(run, ["git", "branch", "--show-current"])
        current = result.output.strip() or "(detached HEAD)"
        await reply_chunks(
            update,
            f"Current branch: {current}\n\nUsage: /branch <name> - switch branches",
        )
        return

    active_text = active_execution_text(context)
    if active_text:
        await reply_chunks(
            update, f"An implementation is already running.\n\n{active_text}"
        )
        return

    branch_name = context.args[0]
    try:
        validate_branch_name(branch_name)
    except ValueError as error:
        await reply_chunks(update, str(error))
        return

    try:
        await asyncio.to_thread(run, ["git", "checkout", branch_name])
    except RuntimeError:
        try:
            await asyncio.to_thread(run, ["git", "fetch", "origin", branch_name])
            await asyncio.to_thread(
                run, ["git", "checkout", "-B", branch_name, f"origin/{branch_name}"]
            )
        except RuntimeError as error:
            await reply_chunks(
                update,
                f"Could not switch to '{branch_name}':\n{redact_sensitive(str(error))}",
            )
            return

    result = await asyncio.to_thread(run, ["git", "status"])
    await reply_chunks(update, f"Switched to branch: {branch_name}\n\n{result.output}")


async def repo_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List registered projects and mark the active project."""
    if not require_authorized(update):
        return
    current = active_project()
    blocks = [{"type": "heading", "size": 2, "text": "Repositories"}]
    for project in list_projects():
        is_active = project.name == current.name
        blocks.append(
            {
                "type": "paragraph",
                "text": [
                    "✓ " if is_active else "",
                    {"type": "bold", "text": project.name},
                    "\nPath: ",
                    {"type": "code", "text": str(project.repo_path)},
                    "\nBranch: ",
                    {"type": "code", "text": project.base_branch},
                    " — active" if is_active else "",
                ],
            }
        )
    blocks.append(
        {
            "type": "paragraph",
            "text": ["Switch with: ", {"type": "code", "text": "/repo_use <name>"}],
        }
    )
    await send_rich_message(update, context, blocks)


async def repo_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register and clone a project from command arguments."""
    if not require_authorized(update):
        return
    if not context.args:
        await prompt_for_arguments(
            update,
            context,
            "repo_add",
            "Enter owner/repo and optionally a local path.",
        )
        return
    repository = context.args[0]
    repo_path = context.args[1] if len(context.args) > 1 else None
    try:
        project, needs_clone = await asyncio.to_thread(
            add_project, repository, repo_path
        )
    except ProjectError as error:
        await reply_chunks(update, f"Could not add project: {error}")
        return

    if needs_clone:
        await reply_chunks(
            update, f"Cloning {project.github_repository} into {project.repo_path} ..."
        )
        try:
            project.repo_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                run,
                [
                    "git",
                    "clone",
                    clone_url(project.github_repository),
                    str(project.repo_path),
                ],
                project.repo_path.parent,
            )
        except RuntimeError as error:
            await reply_chunks(
                update,
                f"Project '{project.name}' was registered, but cloning failed:\n"
                f"{redact_sensitive(str(error))}\n\n"
                f"Clone it manually to {project.repo_path}, or remove it with: /repo_remove {project.name}",
            )
            return

    await reply_chunks(
        update,
        f"Project added: {project.name}\n"
        f"Repository: {project.github_repository}\n"
        f"Path: {project.repo_path}\n"
        f"Base branch: {project.base_branch}\n\n"
        f"Activate it with: /repo_use {project.name}",
    )


async def repo_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch the active project selected by name."""
    if not require_authorized(update):
        return
    if not context.args:
        current = active_project()
        names = [project.name for project in list_projects()]
        keyboard = choice_keyboard("repo_use", names, active=current.name)
        await update.message.reply_text(
            f"Active project: {current.name} ({current.github_repository})\n"
            "Tap to switch:",
            reply_markup=keyboard,
        )
        return

    name = context.args[0]
    try:
        project = await asyncio.to_thread(set_active, name)
    except ProjectError as error:
        await reply_chunks(update, str(error))
        return

    await refresh_bot_name(context, project.name)
    path_present = (project.repo_path / ".git").is_dir()
    warning = (
        ""
        if path_present
        else f"\n\nWarning: {project.repo_path} is not a git repository yet."
    )
    await reply_chunks(
        update,
        f"Active project: {project.name}\n"
        f"Repository: {project.github_repository}\n"
        f"Path: {project.repo_path}\n"
        f"Base branch: {project.base_branch}{warning}",
    )


async def repo_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unregister a project selected by name."""
    if not require_authorized(update):
        return
    if not context.args:
        current = active_project()
        names = [
            project.name for project in list_projects() if project.name != current.name
        ]
        if not names:
            await reply_chunks(update, "There are no inactive projects to remove.")
            return
        await update.message.reply_text(
            "Tap a project to remove:",
            reply_markup=choice_keyboard("repo_remove", names),
        )
        return
    try:
        await asyncio.to_thread(remove_project, context.args[0])
    except ProjectError as error:
        await reply_chunks(update, str(error))
        return
    await reply_chunks(
        update,
        f"Removed project: {context.args[0]}\nActive is now: {active_project().name}",
    )


async def refresh_bot_name(
    context: ContextTypes.DEFAULT_TYPE, project_name: str
) -> None:
    """Cosmetic only. Telegram rate-limits profile changes, so failures are ignored."""
    try:
        await context.bot.set_my_name(name=f"Coding AI Agent - {project_name}"[:64])
    except Exception as error:
        logger.info("Could not update bot name (cosmetic, ignored): %s", error)


async def pull(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pull changes into the active project when no implementation is running."""
    if not require_authorized(update):
        return

    active_text = active_execution_text(context)
    if active_text:
        await reply_chunks(
            update, f"An implementation is already running.\n\n{active_text}"
        )
        return

    try:
        result = await asyncio.to_thread(run, ["git", "pull"])
    except RuntimeError as error:
        await reply_chunks(update, f"git pull failed:\n{redact_sensitive(str(error))}")
        return

    await reply_chunks(update, f"Pull:\n{result.output}")


async def _on_repo_use_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, project_name: str
) -> None:
    """Handle a project-picker button tap: same effect as /repo_use <name>."""
    if not require_authorized(update):
        return
    try:
        project = await asyncio.to_thread(set_active, project_name)
    except ProjectError as error:
        await update.callback_query.edit_message_text(str(error))
        return
    await refresh_bot_name(context, project.name)
    await update.callback_query.edit_message_text(
        f"Active project: {project.name} ({project.github_repository})"
    )


async def _on_repo_remove_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name: str
) -> None:
    if not require_authorized(update):
        return
    try:
        await asyncio.to_thread(remove_project, name)
    except ProjectError as error:
        await update.callback_query.edit_message_text(str(error))
        return
    await update.callback_query.edit_message_text(
        f"Removed project: {name}\nActive is now: {active_project().name}"
    )
