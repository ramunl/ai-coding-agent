"""Manage pending plans, task queues, and execution snapshots.

Every function accepts either a Telegram context (uses _data(context)) or
the user_data mapping itself. That makes state readable from code that has no
Telegram update in hand — e.g. application.user_data[CHAT_ID] in a future
Mini App API — without changing any existing handler call.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

from ai_agent.plan_state import (
    ExecutionState,
    PlanState,
    Verbosity,
    parse_plan_document,
    parse_verbosity,
    render_diff_summary,
)
from ai_agent.planner import normalize_planning_agent
from ai_agent.workflow import ImplementationResult, normalize_implementation_agent

# Saved across restarts. Allowlist on purpose: a key nobody classified is
# forgotten on restart (harmless) rather than restored (possibly harmful).
PERSISTENT_KEYS = frozenset(
    {
        "pending_plan",
        "pending_implementation",
        "pending_bugfix_clarification",
        "task_queue",
        "next_task_id",
        "verbosity",
        "planning_agent",
        "implementation_agent",
        "last_execution",
    }
)

# Describe THIS process only. Restoring them after a restart would lie:
# queue_runner_active=True would stop the queue from ever running again, and
# active_execution would report work that died with the old process.
TRANSIENT_KEYS = frozenset(
    {"active_execution", "queue_runner_active", "argument_prompt"}
)


def _data(source: Any) -> MutableMapping:
    """Return the user-data mapping from a context or from the mapping itself."""
    return source.user_data if hasattr(source, "user_data") else source


def get_verbosity(context: ContextTypes.DEFAULT_TYPE) -> Verbosity:
    """Read the reply verbosity, falling back to concise output."""
    value = _data(context).get("verbosity", Verbosity.CONCISE.value)
    return parse_verbosity(str(value)) or Verbosity.CONCISE


def set_pending_from_plan(
    context: ContextTypes.DEFAULT_TYPE, plan_state: PlanState
) -> None:
    """Convert an approved plan into pending implementation data."""
    document = parse_plan_document(plan_state.plan_text, plan_state.feature)
    _data(context)["pending_implementation"] = {
        "change": plan_state.feature,
        "codex_prompt": document.codex_prompt,
        "branch_name": document.branch,
        "commit_type": "feat",
        "pr_body_label": f"Plan revision {plan_state.revision}",
        "confirmation_label": "implementation",
    }


def next_task_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    """Allocate the next task identifier for this user."""
    task_id = int(_data(context).get("next_task_id", 1))
    _data(context)["next_task_id"] = task_id + 1
    return task_id


def task_queue(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    """Return the user queue, initializing it when absent."""
    queue = _data(context).get("task_queue")
    if not isinstance(queue, list):
        queue = []
        _data(context)["task_queue"] = queue
    return queue


def enqueue_pending_implementation(
    context: ContextTypes.DEFAULT_TYPE, pending: dict
) -> dict:
    """Snapshot pending work and append it to the FIFO queue."""
    task = {
        "id": next_task_id(context),
        "change": pending["change"],
        "codex_prompt": pending["codex_prompt"],
        "branch_name": pending["branch_name"],
        "commit_type": pending.get("commit_type", "feat"),
        "pr_body_label": pending.get("pr_body_label", "Plan"),
        "confirmation_label": pending.get("confirmation_label", "implementation"),
        "implementation_agent": pending.get("implementation_agent")
        or current_implementation_agent(context),
    }
    task_queue(context).append(task)
    return task


def render_task_queue(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Describe the running task and pending queue in order."""
    queue = task_queue(context)
    active_text = active_execution_text(context)
    if not queue and not active_text:
        return "Task queue is empty."

    lines = ["Task queue:"]
    if active_text:
        active = _data(context).get("active_execution") or {}
        lines.extend(
            [
                "",
                f"Running: {active.get('branch', 'unknown')}",
                f"Phase: {active.get('phase', 'working')}",
            ]
        )
    if queue:
        lines.append("")
        lines.append("Pending:")
        for index, task in enumerate(queue, 1):
            lines.append(
                (
                    f"{index}. #{task['id']} {task['branch_name']} "
                    f"({task.get('confirmation_label', 'implementation')})"
                )
            )
    return "\n".join(lines)


def forget_pending_implementation(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Discard the pending implementation without changing queued tasks."""
    _data(context).pop("pending_implementation", None)


def last_execution(context: ContextTypes.DEFAULT_TYPE) -> ExecutionState | None:
    """Return the most recent captured execution, if available."""
    execution = _data(context).get("last_execution")
    return execution if isinstance(execution, ExecutionState) else None


def set_active_execution(
    context: ContextTypes.DEFAULT_TYPE,
    branch: str,
    phase: str,
    status_text: str = "RUNNING",
) -> None:
    """Record the branch, phase, and status of running work."""
    _data(context)["active_execution"] = {
        "branch": branch,
        "phase": phase,
        "status": status_text,
    }


def active_execution_text(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Describe running work, or return None when idle."""
    execution = _data(context).get("active_execution")
    if not isinstance(execution, dict):
        return None
    branch = execution.get("branch") or "unknown"
    phase = execution.get("phase") or "working"
    status_text = execution.get("status") or "RUNNING"
    return (
        f"Implementation status:\n{status_text}\n\nBranch:\n{branch}\n\nPhase:\n{phase}"
    )


def update_last_execution(
    context: ContextTypes.DEFAULT_TYPE,
    branch_name: str,
    result: ImplementationResult,
    pr_url: str,
    tests: str = "PENDING",
) -> None:
    """Capture implementation output for later inspection."""
    _data(context)["last_execution"] = ExecutionState(
        branch=branch_name,
        files_changed=result.files_changed,
        diff_summary=render_diff_summary(result.diff, result.files_changed),
        full_diff=result.diff,
        logs=result.output,
        pr_url=pr_url,
        tests=tests,
    )


def current_implementation_agent(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Resolve the user-selected implementation provider."""
    return normalize_implementation_agent(
        str(_data(context).get("implementation_agent", "")) or None
    )


def current_planning_agent(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Resolve the user-selected planning provider."""
    return normalize_planning_agent(
        str(_data(context).get("planning_agent", "")) or None
    )


def snapshot(context: Any) -> dict:
    """Plain, JSON-serializable view of the agent state for non-chat clients.

    This is the read model a Mini App or HTTP API would serve. It exposes what
    a UI needs to render and deliberately omits bulky or sensitive fields
    (full diffs, logs, generated prompts).
    """
    data = _data(context)
    plan = data.get("pending_plan")
    pending = data.get("pending_implementation")
    execution = data.get("active_execution")
    last = last_execution(context)
    return {
        "running": dict(execution) if isinstance(execution, dict) else None,
        "queue": [
            {
                "id": task.get("id"),
                "branch": task.get("branch_name"),
                "label": task.get("confirmation_label", "implementation"),
                "agent": task.get("implementation_agent"),
            }
            for task in task_queue(context)
        ],
        "pending_plan": (
            {
                "id": plan.id,
                "feature": plan.feature,
                "revision": plan.revision,
                "approved": plan.approved,
            }
            if isinstance(plan, PlanState)
            else None
        ),
        "pending_branch": pending.get("branch_name")
        if isinstance(pending, dict)
        else None,
        "awaiting_bugfix_answer": isinstance(
            data.get("pending_bugfix_clarification"), dict
        ),
        "planning_agent": current_planning_agent(context),
        "implementation_agent": current_implementation_agent(context),
        "verbosity": get_verbosity(context).value,
        "last_execution": (
            {
                "branch": last.branch,
                "pr_url": last.pr_url,
                "tests": last.tests,
                "files_changed": list(last.files_changed),
            }
            if last
            else None
        ),
    }
