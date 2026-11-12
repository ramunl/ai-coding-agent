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

from ai_agent.context_projection import task_context_status
from ai_agent.task_groups import group_title
from ai_agent.task_images import remove
from ai_agent.task_reopen import attempt_history, can_reopen

# "ended": the run is no longer active; task_pulls checks its outcome on GitHub
# because cached execution results cannot reliably identify the latest run.
# "proposed": drafted by the worker from the server logs; the owner accepts it
# (it becomes "todo") or dismisses it. The worker never starts a proposal.
STAGES = (
    "proposed",
    "todo",
    "planning",
    "planned",
    "implementing",
    "ended",
    "blocked",
    "ops_required",
    "pr",
    "done",
    "stopped",
)
ENDED_NOTE = "run finished; checking GitHub for a pull request"
# Saved by an earlier version, which wrongly called such a task "PR open".
_OLD_UNKNOWN_NOTE = "run finished; pull request not recorded"
FINISHED = ("pr", "done", "stopped")
# What a task keeps of its run, so a run that failed can be retried with the
# same approved plan (and another tool) instead of planning it again.
RUN_KEYS = (
    "verification_profile",
    "context_spec",
    "context_packet",
    "packet_metadata",
    "dependency_task",
    "approval_binding",
    "run_binding",
    "repository_identity",
    "change",
    "images",
    "codex_prompt",
    "branch_name",
    "commit_type",
    "pr_body_label",
    "confirmation_label",
    "implementation_agent",
    "implementation_model",
    "implementation_ai",
)
RUN_PROMPT_LIMIT = 20000
RETRYABLE = ("stopped", "ended", "blocked", "ops_required")
MAX_TASKS = 50
TITLE_LENGTH = 300


def tasks_of(data: MutableMapping) -> list[dict]:
    """The task list, created on first use."""
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
        data["tasks"] = tasks
    for task in tasks:
        task.setdefault("images", [])
    return tasks


def find_task(data: MutableMapping, task_id: str) -> dict | None:
    """The task with this id, or None."""
    return next((task for task in tasks_of(data) if task.get("id") == task_id), None)


def add_task(
    data: MutableMapping,
    title: str,
    repo: str,
    todo: str | None,
    images: list[dict] | None = None,
) -> dict:
    """Add a todo-stage task; the oldest finished ones make room past MAX_TASKS."""
    tasks = tasks_of(data)
    task = {
        "id": secrets.token_hex(4),
        "title": " ".join(title.split())[:TITLE_LENGTH],
        "text": " ".join(title.split())[:4000],
        "repo": repo,
        "todo": todo,
        "images": list(images or []),
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
        remove_task(data, oldest)
    return task


def set_stage(task: dict, stage: str, note: str | None = None) -> None:
    """Move a task to a stage, with an optional reason shown next to it."""
    if stage != task.get("stage") and stage in ("done", "stopped"):
        task["finished_at"] = time.time()
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
    changed = False
    for task in tasks_of(data):
        before = (task.get("stage"), task.get("pr_url"), task.get("note"))
        _advance(task, data, pending_id, busy)
        changed |= before != (task.get("stage"), task.get("pr_url"), task.get("note"))
    return changed


def _advance(task: dict, data: MutableMapping, pending_id: Any, busy: set) -> None:
    stage = task.get("stage")
    if stage == "pr" and task.get("note") == _OLD_UNKNOWN_NOTE:
        set_stage(task, "ended", ENDED_NOTE)
    elif stage == "planning" and data.get("planning_task") != task.get("id"):
        set_stage(task, "todo", "planning was interrupted")
    elif stage == "planned":
        if task.get("branch") in busy:
            set_stage(task, "implementing")
        elif pending_id != task.get("plan_id"):
            if task.get("run") or task.get("context_handoff"):
                set_stage(task, "ended", ENDED_NOTE)
            else:
                set_stage(task, "stopped", "the plan was cancelled or replaced")
    elif stage == "implementing" and task.get("branch") not in busy:
        # last_execution can belong to an earlier run on the same branch (and
        # survives restarts). Only GitHub can confirm this branch's outcome.
        set_stage(task, "ended", ENDED_NOTE)


def remember_run(data: MutableMapping, item: dict) -> None:
    """Keep a queued run on the task of its branch, for a later retry."""
    repo = (item.get("repository_identity") or {}).get("project_name")
    candidates = [
        task
        for task in tasks_of(data)
        if task.get("branch") == item.get("branch_name")
        and task.get("stage") in ("planned", "implementing")
        and (not repo or task.get("repo") == repo)
        and (
            not item.get("checkpoint_task") or task.get("id") == item["checkpoint_task"]
        )
        and (not item.get("plan_id") or task.get("plan_id") == item["plan_id"])
    ]
    task = candidates[0] if len(candidates) == 1 else None
    if task is None:
        return
    item["checkpoint_task"] = task["id"]
    from copy import deepcopy

    item["dependency_task"] = deepcopy(
        {
            key: task[key]
            for key in (
                "id",
                "after",
                "dependencies",
                "dependency_ids",
                "attempt",
                "dependency_invalidations",
            )
            if key in task
        }
    )
    run = {key: item.get(key) for key in RUN_KEYS}
    if not run.get("run_binding"):
        # Legacy records keep their existing preview bound. Sealed approvals
        # must retain exact requirements for future Resume/Start fresh actions.
        run["codex_prompt"] = str(run.get("codex_prompt") or "")[:RUN_PROMPT_LIMIT]
    task.pop("implementation_ai", None)
    task["run"] = run


def can_retry(task: dict) -> bool:
    """True when the task's run failed and its approved plan is kept."""
    return task.get("stage") in RETRYABLE and isinstance(task.get("run"), dict)


def waiting_for(task: dict, data: MutableMapping) -> dict | None:
    """Display the latest dependency decision without performing evidence I/O."""
    from ai_agent.dependency_evidence import cached_reason

    reason = cached_reason(task, data)
    return {"title": reason} if reason else None


def can_resolve_blockage(task: dict, data: dict) -> bool:
    """Advertise an evidence check, not a promise that resolution will succeed."""
    return bool(
        task.get("stage") in {"ended", "pr", "stopped", "blocked"}
        and task.get("pr_url")
        and task.get("branch")
        and (task.get("run") or {}).get("context_spec")
        and (
            data.get("context_recovery_pending")
            or task.get("stage") == "ended"
            or not task.get("context_handoff")
        )
        and not data.get("active_execution")
        and data.get("planning_task") != task.get("id")
    )


def view(task: dict, data: MutableMapping, *, now: int | None = None) -> dict:
    """What the dashboard shows for one task."""
    pending = data.get("pending_plan")
    is_own_plan = task.get("stage") == "planned" and getattr(
        pending, "id", None
    ) == task.get("plan_id")
    from ai_agent.checkpoints import task_status
    from ai_agent.implementation_stop import status as stop_status

    stopping = (
        task.get("stop_requested")
        and task.get("stage") == "implementing"
        and (data.get("active_execution") or {}).get("branch") == task.get("branch")
    )
    stop = stop_status(task.get("id", ""), task.get("branch", ""))
    if stopping:
        stop.update(can_stop=True, stopping=True)
    from ai_agent.task_ai import task_ai

    ai = task_ai(task)
    if is_own_plan:
        from ai_agent.task_ai import public_selection

        ai["planner"] = (
            public_selection(getattr(pending, "planning_ai", None)) or ai["planner"]
        )
    return {
        "ai": ai,
        **stop,
        **(
            task_status(task)
            if can_retry(task)
            else {
                "can_resume": False,
                "resume_reason": "Task has no failed attempt.",
                "resume_summary": "",
            }
        ),
        "context_status": task_context_status(
            task, data, now=int(time.time()) if now is None else now
        ),
        "id": task.get("id"),
        "title": task.get("title"),
        "image_count": len(task.get("images") or []),
        "repo": task.get("repo"),
        "todo": task.get("todo"),
        "stage": task.get("stage"),
        "note": task.get("note"),
        "branch": task.get("branch"),
        "pr_url": task.get("pr_url"),
        "merged_at": task.get("merged_at"),
        "merge_commit": task.get("merge_commit"),
        "pr_evidence": task.get("pr_evidence"),
        "created_at": task.get("created_at"),
        "source": task.get("source"),
        "evidence": list(task.get("evidence") or []),
        "group": task.get("group"),
        "group_title": group_title(task),
        "part": task.get("part"),
        "parts": task.get("parts"),
        "waiting_for": (waiting_for(task, data) or {}).get("title"),
        "dependency_reason": (waiting_for(task, data) or {}).get("title")
        or (task.get("dependency_check") or {}).get("reason"),
        "can_retry": can_retry(task),
        "can_reopen": can_reopen(task, data),
        "can_resolve_blockage": can_resolve_blockage(task, data),
        "blockage_resolved": bool(task.get("blockage_resolution")),
        "can_reconcile": bool(
            task.get("context_handoff") or task.get("context_handoffs")
        )
        and task.get("stage") in {"todo", "stopped"},
        "attempt": task.get("attempt", 1),
        "attempt_history": attempt_history(task),
        "autopilot": bool(task.get("autopilot")),
        "queued": bool(task.get("start_next")),
        "blocks": any(
            other.get("after") == task.get("id") and other.get("stage") == "todo"
            for other in tasks_of(data)
        )
        and task.get("stage") not in ("pr", "done"),
        "plan_revision": getattr(pending, "revision", None) if is_own_plan else None,
        "plan_approved": bool(getattr(pending, "approved", False))
        if is_own_plan
        else None,
    }


def remove_task(data: MutableMapping, task: dict) -> None:
    """Remove a task and attachments not shared with another task or plan."""
    from ai_agent.dependency_evidence import bind_dependencies

    for consumer in tasks_of(data):
        try:
            bind_dependencies(consumer, data)
        except (ValueError, TypeError, KeyError):
            # Preserve unresolved edges: removal cannot grant readiness.
            pass
    tasks_of(data).remove(task)
    retained = [*tasks_of(data), *(data.get("task_queue") or [])]
    pending = data.get("pending_plan")
    if pending is not None:
        retained.append({"images": getattr(pending, "images", [])})
    retained.append(data.get("pending_implementation") or {})
    ids = {ref.get("id") for item in retained for ref in item.get("images", [])}
    remove([ref for ref in task.get("images", []) if ref.get("id") not in ids])
