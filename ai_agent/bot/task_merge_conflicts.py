"""Resolve the saved PR of an existing task, in that task's repository."""

from typing import Any

from telegram.ext import CallbackContext

from ai_agent.actions import ActionError, busy_reason
from ai_agent.bot import task_actions, work_actions
from ai_agent.bot.merge_conflicts import fix_merge_conflicts
from ai_agent.github_links import parse_github_reference
from ai_agent.merge_conflict import task_merge_conflict_url
from ai_agent.projects import ProjectError, get_project
from ai_agent.tasks import find_task


def _saved_target(task: dict) -> tuple[str, str]:
    """Validate the saved PR against the task's registered repository."""
    url = task_merge_conflict_url(task)
    if url is None:
        raise ActionError(
            "Only a PR-open task with a saved PR URL can resolve conflicts."
        )
    try:
        project = get_project(task.get("repo", ""))
    except ProjectError as error:
        raise ActionError(str(error)) from error
    reference = parse_github_reference(url)
    if (
        f"{reference.owner}/{reference.repo}".lower()
        != project.github_repository.lower()
    ):
        raise ActionError("The saved PR URL does not belong to this task's repository.")
    return project.name, url


async def run(ptb_app: Any, owner_id: int, task: dict) -> str:
    """Start the common resolver using server-owned task data, never a client URL."""
    data = ptb_app.user_data[owner_id]
    reason = task_actions.start_refusal(data) or busy_reason(data)
    if reason:
        raise ActionError(f"Cannot resolve task conflicts: {reason}.")
    repo, url = _saved_target(task)
    await task_actions._switch_to(ptb_app, data, repo)
    # A repository switch awaits I/O: refuse a task removed or changed meanwhile.
    if find_task(data, task["id"]) is not task or _saved_target(task) != (repo, url):
        raise ActionError("The task changed; refresh before resolving conflicts.")
    update = work_actions.chat_update(ptb_app.bot, owner_id)
    context = CallbackContext(ptb_app, chat_id=owner_id, user_id=owner_id)
    context.args = [url]

    async def handler(update: Any, context: Any) -> None:
        await fix_merge_conflicts(update, context, task_id=task["id"])

    await work_actions._start(handler, update, context)
    return work_actions.summary(
        update.message,
        "Resolving task PR conflicts; progress is in Tasks and Telegram.",
    )
