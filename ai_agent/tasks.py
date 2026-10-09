"""Tasks: work made from a todo, followed from planning to a pull request.

A task names a repository and the text to plan. The agent works on one plan
at a time, so a task waits as "todo" until it can be planned. After that its
stage follows what the agent observably does with the task's plan and branch
(see sync): no handler has to remember to update it, and work started or
cancelled from the chat moves the task too.

Stages: todo -> planning -> planned -> implementing -> pr -> done (merged, see
task_pulls); "stopped" when the plan was cancelled or replaced, the run ended
without a pull request, or the pull request was closed without merging.
Stored in user_data["tasks"] (persistent); user_data["planning_task"] is the
id being planned right now and is never saved, so a restart cannot leave a
task stuck in planning.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import MutableMapping
from typing import Any

STAGES = ("todo", "planning", "planned", "implementing", "pr", "done", "stopped")
FINISHED = ("pr", "done", "stopped")
MAX_TASKS = 50
TITLE_LENGTH = 300


def tasks_of(data: MutableMapping) -> list[dict]:
    """The task list, created on first use."""
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
        data["tasks"] = tasks
    return tasks


def find_task(data: MutableMapping, task_id: str) -> dict | None:
    """The task with this id, or None."""
    return next((task for task in tasks_of(data) if task.get("id") == task_id), None)


def add_task(data: MutableMapping, title: str, repo: str, todo: str | None) -> dict:
    """Add a todo-stage task; the oldest finished ones make room past MAX_TASKS."""
    tasks = tasks_of(data)
    task = {
        "id": secrets.token_hex(4),
        "title": " ".join(title.split())[:TITLE_LENGTH],
        "text": " ".join(title.split())[:4000],
        "repo": repo,
        "todo": todo,
        "stage": "todo",
        "created_at": time.time(),
        "plan_id": None,
        "branch": None,
        "pr_url": None,
        "note": None,
    }
    tasks.append(task)
    while len(tasks) > MAX_TASKS:
        oldest = next((item for item in tasks if item.get("stage") in FINISHED), None)
        if oldest is None:
            break
        tasks.remove(oldest)
    return task


def set_stage(task: dict, stage: str, note: str | None = None) -> None:
    """Move a task to a stage, with an optional reason shown next to it."""
    task["stage"] = stage
    task["note"] = note


def _queued_branches(data: MutableMapping) -> set[str]:
    branches = {str(item.get("branch_name")) for item in data.get("task_queue") or []}
    running = data.get("active_execution")
    if isinstance(running, dict) and running.get("branch"):
        branches.add(str(running["branch"]))
    return branches


def sync(data: MutableMapping) -> bool:
    """Move tasks along by what happened to their plan and branch; True if any."""
    pending = data.get("pending_plan")
    pending_id = getattr(pending, "id", None)
    busy = _queued_branches(data)
    last = data.get("last_execution")
    changed = False
    for task in tasks_of(data):
        before = (task.get("stage"), task.get("pr_url"), task.get("note"))
        _advance(task, data, pending_id, busy, last)
        changed |= before != (task.get("stage"), task.get("pr_url"), task.get("note"))
    return changed


def _advance(
    task: dict, data: MutableMapping, pending_id: Any, busy: set, last: Any
) -> None:
    stage = task.get("stage")
    if stage == "planning" and data.get("planning_task") != task.get("id"):
        set_stage(task, "todo", "planning was interrupted")
    elif stage == "planned":
        if task.get("branch") in busy:
            set_stage(task, "implementing")
        elif pending_id != task.get("plan_id"):
            set_stage(task, "stopped", "the plan was cancelled or replaced")
    elif stage == "implementing" and task.get("branch") not in busy:
        if getattr(last, "branch", None) != task.get("branch"):
            set_stage(task, "pr", "run finished; pull request not recorded")
        elif getattr(last, "pr_url", None):
            task["pr_url"] = last.pr_url
            set_stage(task, "pr")
        else:
            set_stage(task, "stopped", "the run ended without a pull request")


def view(task: dict, data: MutableMapping) -> dict:
    """What the dashboard shows for one task."""
    pending = data.get("pending_plan")
    is_own_plan = task.get("stage") == "planned" and getattr(
        pending, "id", None
    ) == task.get("plan_id")
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "repo": task.get("repo"),
        "todo": task.get("todo"),
        "stage": task.get("stage"),
        "note": task.get("note"),
        "branch": task.get("branch"),
        "pr_url": task.get("pr_url"),
        "merged_at": task.get("merged_at"),
        "merge_commit": task.get("merge_commit"),
        "created_at": task.get("created_at"),
        "plan_revision": getattr(pending, "revision", None) if is_own_plan else None,
        "plan_approved": bool(getattr(pending, "approved", False))
        if is_own_plan
        else None,
    }
