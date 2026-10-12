"""The autonomous worker's rules: what to do next, and when not to.

The worker takes To do tasks one at a time: it plans one, waits for the owner
to approve the plan, then runs it to a pull request. It never merges or
deploys. Its settings and counters live in user_data["worker"] (saved); this
module only decides, worker_loop carries the decision out.

Budget, both changeable from the dashboard or /worker:
- max_runs: implementation runs per rolling 24 h (0 = no cap);
- reserve: percent of a Codex limit window it must leave for the owner.
After MAX_FAILURES failed tasks in a row it switches itself off.
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from ai_agent.planner import normalize_planning_agent
from ai_agent.workflow import normalize_implementation_agent

DEFAULTS = {"enabled": False, "max_runs": 3, "reserve": 30}
MAX_FAILURES = 3
DAY = 24 * 3600


@dataclass(frozen=True)
class Decision:
    """What the worker does on this tick: an action, or why it waits."""

    action: str  # "wait", "plan" or "run"
    status: str
    task: dict | None = None
    tool: str | None = None


def settings(data: MutableMapping) -> dict:
    """The worker's saved settings and counters, with defaults filled in."""
    worker = data.get("worker")
    if not isinstance(worker, dict):
        worker = {}
        data["worker"] = worker
    for key, value in DEFAULTS.items():
        worker.setdefault(key, value)
    worker.setdefault("runs", [])
    worker.setdefault("failures", 0)
    worker.setdefault("current", None)
    return worker


def runs_today(worker: dict, now: float) -> int:
    """Implementation runs started in the last 24 hours."""
    return sum(1 for at in worker.get("runs", []) if now - at < DAY)


def short_window(limits: dict, tool: str, reserve: int, now: float) -> dict | None:
    """The first limit window of tool below the reserve, or None if there is room.

    Codex reports its windows; Claude Code's are readable only from an
    interactive session, so without a reading Claude is assumed available.
    """
    reading = limits.get("codex" if tool == "codex" else "claude_code") or {}
    for window in reading.get("windows") or []:
        remaining = window.get("remaining_percent")
        resets = window.get("resets_at")
        if not isinstance(remaining, (int, float)):
            continue
        if isinstance(resets, (int, float)) and resets <= now:
            continue  # that window has reset since the reading
        if remaining < reserve:
            return window
    return None


def _tool_wait(limits: dict, tool: str, reserve: int, now: float) -> str | None:
    window = short_window(limits, tool, reserve, now)
    if window is None:
        return None
    left = window.get("remaining_percent")
    resets = window.get("resets_at")
    until = (
        time.strftime(" until %H:%M", time.localtime(resets))
        if isinstance(resets, (int, float))
        else ""
    )
    return f"waiting: {tool} has {left}% left, below the {reserve}% reserve{until}"


def _approved_task(data: MutableMapping, tasks: list[dict]) -> dict | None:
    """The task whose approved plan is waiting to run, if any."""
    plan = data.get("pending_plan")
    if not plan or not getattr(plan, "approved", False):
        return None
    if not data.get("pending_implementation"):
        return None
    return next(
        (
            task
            for task in tasks
            if task.get("stage") == "planned" and task.get("plan_id") == plan.id
        ),
        None,
    )


def decide(
    data: MutableMapping, tasks: list[dict], limits: dict, now: float
) -> Decision:
    """The next step, given the agent's state, the tasks and the limits."""
    worker = settings(data)
    if not worker["enabled"]:
        return Decision("wait", "off")
    if data.get("planning_task") or data.get("thinking"):
        return Decision("wait", "waiting: the agent is planning")
    busy = data.get("active_execution") or data.get("task_queue")
    if busy or data.get("queue_runner_active"):
        return Decision("wait", "waiting: a run is in progress")
    reserve = int(worker["reserve"])
    approved = _approved_task(data, tasks)
    if approved is not None:
        cap = int(worker["max_runs"])
        if cap and runs_today(worker, now) >= cap:
            return Decision("wait", f"waiting: {cap} runs today, the daily cap")
        tool = normalize_implementation_agent(data.get("implementation_agent"))
        reason = _tool_wait(limits, tool, reserve, now)
        if reason:
            return Decision("wait", reason)
        return Decision("run", f"running {approved['title']}", approved, tool)
    plan = data.get("pending_plan")
    if plan is not None:
        title = getattr(plan, "feature", "the plan")
        return Decision("wait", f"waiting for your approval: {title}")
    if data.get("pending_implementation") or data.get("pending_bugfix_clarification"):
        return Decision("wait", "waiting: other work is pending")
    todo = next(
        (
            task
            for task in tasks
            if task.get("stage") == "todo" and not task.get("worker_failed")
        ),
        None,
    )
    if todo is None:
        return Decision("wait", "idle: no To do tasks")
    tool = normalize_planning_agent(data.get("planning_agent"))
    reason = _tool_wait(limits, tool, reserve, now)
    if reason:
        return Decision("wait", reason)
    return Decision("plan", f"planning {todo['title']}", todo, tool)


def follow_up(worker: dict, task: dict | None) -> str | None:
    """How the worker's current task went: "ok", "failed", or None (not yet)."""
    current = worker.get("current")
    if not current:
        return None
    if task is None:
        return "gone"
    stage = task.get("stage")
    if current.get("phase") == "planning":
        if stage == "planning":
            return None
        return (
            "ok"
            if stage in ("planned", "implementing", "ended", "pr", "done")
            else "failed"
        )
    if stage in ("planned", "implementing", "ended"):
        return None
    return "ok" if stage in ("pr", "done") else "failed"


def view(data: MutableMapping, now: float) -> dict:
    """What the dashboard shows about the worker."""
    worker = data.get("worker") if isinstance(data.get("worker"), dict) else {}
    merged: dict[str, Any] = {**DEFAULTS, **worker}
    return {
        "enabled": bool(merged["enabled"]),
        "max_runs": int(merged["max_runs"]),
        "reserve": int(merged["reserve"]),
        "runs_today": runs_today(merged, now),
        "failures": int(merged.get("failures", 0)),
        "status": data.get("worker_status")
        or ("off" if not merged["enabled"] else "starting"),
        "current_task": (merged.get("current") or {}).get("task"),
    }
