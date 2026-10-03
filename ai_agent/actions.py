"""What the agent does when asked to change its setup, whoever asks.

Telegram commands, their button taps, and dashboard requests (via the inbox)
all call these functions, so the rules live in one place. Each action
validates its input and raises ActionError with a message fit for the user.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping

from ai_agent.ai_tools import get_tool
from ai_agent.config import redact_sensitive
from ai_agent.planner import normalize_planning_agent, planning_agent_label
from ai_agent.projects import (
    Project,
    ProjectError,
    add_project,
    clone_url,
    set_active,
)
from ai_agent.self_update import schedule_restart
from ai_agent.shell import run
from ai_agent.workflow import implementation_agent_label, normalize_implementation_agent


class ActionError(Exception):
    """The action was refused or failed; the message explains why."""


def busy_reason(user_data: MutableMapping) -> str | None:
    """Why the active project must not change now, or None when idle.

    Switching mid-task would point running or queued work at another repo.
    """
    if user_data.get("active_execution"):
        return "a task is running"
    queued = len(user_data.get("task_queue") or [])
    if queued:
        return f"{queued} task(s) are queued"
    return None


async def use_project(name: str, user_data: MutableMapping) -> Project:
    """Make a registered project active, unless work is running or queued."""
    reason = busy_reason(user_data)
    if reason:
        raise ActionError(f"Cannot switch projects while {reason}.")
    try:
        return await asyncio.to_thread(set_active, name)
    except ProjectError as error:
        raise ActionError(str(error)) from error


async def add_repository(
    repository: str, repo_path: str | None = None
) -> tuple[Project, str]:
    """Register a repository and clone it if needed; return it and a summary.

    A failed clone keeps the registration and says how to finish by hand,
    exactly like /repo_add.
    """
    try:
        project, needs_clone = await asyncio.to_thread(
            add_project, repository, repo_path
        )
    except ProjectError as error:
        raise ActionError(f"Could not add project: {error}") from error
    if needs_clone:
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
            raise ActionError(
                f"Project '{project.name}' was registered, but cloning failed: "
                f"{redact_sensitive(str(error))}. Clone it manually to "
                f"{project.repo_path}, or remove it with /repo_remove {project.name}"
            ) from error
    return project, f"Project added: {project.name} ({project.github_repository})"


def set_planner(value: str, user_data: MutableMapping) -> str:
    """Choose who writes plans; return the confirmation text."""
    try:
        selected = normalize_planning_agent(value)
    except ValueError as error:
        raise ActionError(str(error)) from error
    user_data["planning_agent"] = selected
    return f"Planning agent set to {planning_agent_label(selected)}."


def set_implementer(value: str, user_data: MutableMapping) -> str:
    """Choose who implements; return the confirmation text."""
    try:
        selected = normalize_implementation_agent(value)
    except ValueError as error:
        raise ActionError(str(error)) from error
    user_data["implementation_agent"] = selected
    return f"Implementation agent set to {implementation_agent_label(selected)}."


async def switch_model(tool_name: str, model: str) -> str:
    """Verify a model, save it, and restart; refuse read-only tools.

    Returns the confirmation text. The restart is scheduled, not immediate,
    so callers can record the result first.
    """
    tool = get_tool(tool_name)
    if tool is None:
        raise ActionError(f"Unknown AI tool '{tool_name}'.")
    if not tool.manageable:
        raise ActionError(f"{tool.name} is read-only here. {tool.info().note}")
    reachable, detail = await asyncio.to_thread(tool.verify, model)
    if not reachable:
        raise ActionError(
            f"Refusing to switch: {model} is {detail}. The current model is unchanged."
        )
    await asyncio.to_thread(tool.set_model, model)
    try:
        restart_note = await asyncio.to_thread(schedule_restart)
    except RuntimeError as error:
        raise ActionError(str(error)) from error
    return f"Verified and saved {tool.name} model = {model}. {restart_note}"
