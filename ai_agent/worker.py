"""The autonomous worker's rules: what to do next, and when not to.

The worker takes To do tasks one at a time: it plans one, waits for the owner
to approve the plan, then runs it to a pull request. It never merges or
deploys. Its settings and counters live in user_data["worker"] (saved); this
module only decides, worker_loop carries the decision out.

Budget, both changeable from the dashboard or /worker:
- max_runs: implementation runs per rolling 24 h (0 = no cap);
- reserve: percent of a Codex limit window it must leave for the owner.
After MAX_FAILURES failed tasks in a row it switches itself off.

Tools: with auto_tool on (the default) it uses the chosen planner or
implementer while that tool has room, otherwise the other one. "Room" means
above the reserve in the tool's last limits reading, and not marked out: a
run that fails with a usage-limit error marks its tool out until the reset
time in the error (an hour if it gives none). Claude Code's limits are not
readable from the server, so for Claude that error is the only signal.
"""

from __future__ import annotations

import re
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from ai_agent.plan_state import parse_plan_document
from ai_agent.planner import normalize_planning_agent
from ai_agent.projects import active_project
from ai_agent.tasks import waiting_for
from ai_agent.workflow import normalize_implementation_agent

DEFAULTS = {
    "enabled": False,
    "max_runs": 3,
    "reserve": 30,
    "auto_tool": True,
    "review_proposals": 3,  # per daily code review; 0 = no reviews
}
TOOLS = ("codex", "claude")
LIMIT_FALLBACK_SECONDS = 3600
MAX_FAILURES = 3
DAY = 24 * 3600
HISTORY_DAYS = 7
HISTORY_LIMIT = 200
# How a finished step of the worker went, as kept in its history.
OUTCOMES = ("planned", "pr", "failed", "limit")


@dataclass(frozen=True)
class Decision:
    """What the worker does on this tick: an action, or why it waits."""

    action: str  # "wait", "plan", "approve"/"split" (autopilot) or "run"
    status: str
    # Keep the whole persisted task, including images, when selecting/falling back.
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
    if not isinstance(worker.get("out_until"), dict):
        worker["out_until"] = {}
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


def _clock(seconds: float) -> str:
    return time.strftime("%H:%M", time.localtime(seconds))


def _tool_wait(
    limits: dict, tool: str, reserve: int, now: float, worker: dict | None = None
) -> str | None:
    out = (worker or {}).get("out_until", {}).get(tool)
    if isinstance(out, (int, float)) and out > now:
        return f"{tool} hit its usage limit; out until {_clock(out)}"
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


def pick_tool(
    preferred: str, limits: dict, worker: dict, now: float
) -> tuple[str | None, str]:
    """The tool to use (preferred first, the other if auto_tool), or None and why."""
    reserve = int(worker["reserve"])
    order = [preferred]
    if worker.get("auto_tool", True):
        order += [tool for tool in TOOLS if tool != preferred]
    reasons = []
    for tool in order:
        reason = _tool_wait(limits, tool, reserve, now, worker)
        if reason is None:
            note = f" ({reasons[0].removeprefix('waiting: ')})" if reasons else ""
            return tool, note
        reasons.append(reason)
    return None, "waiting: " + "; ".join(r.removeprefix("waiting: ") for r in reasons)


# Usage-limit failures as the CLIs word them, and where they say the reset is.
_LIMIT_ERROR = re.compile(
    r"usage limit|limit reached|hit your (usage )?limit|rate.?limit|quota exceeded",
    re.IGNORECASE,
)
_RESET_EPOCH = re.compile(r"\|(\d{10})\b")
_RESET_IN = re.compile(
    r"(?:try again|retry|resets?) in (\d+)\s*(minute|min|hour|h)", re.IGNORECASE
)
_RESET_AT = re.compile(
    r"resets?(?: at)? (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE
)


def limit_reset(text: str, now: float) -> float | None:
    """When a usage-limit error says the limit resets; None if it is no such error."""
    if not text or not _LIMIT_ERROR.search(text):
        return None
    if match := _RESET_EPOCH.search(text):
        return float(match.group(1))
    if match := _RESET_IN.search(text):
        amount = int(match.group(1))
        return now + amount * (3600 if match.group(2).lower().startswith("h") else 60)
    if match := _RESET_AT.search(text):
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            local = time.localtime(now)
            target = time.mktime((*local[:3], hour, minute, 0, 0, 0, -1))
            return target if target > now else target + DAY
    return now + LIMIT_FALLBACK_SECONDS


def _needs_split(plan: Any) -> bool:
    """True when the plan is for other repositories than the active one."""
    names = [
        item["name"]
        for item in parse_plan_document(
            getattr(plan, "plan_text", ""), getattr(plan, "feature", "")
        ).repositories
    ]
    if not names:
        return False
    try:
        active = active_project().name
    except Exception:  # no registry: leave the plan to the owner
        return False
    return len(names) > 1 or names != [active]


def _plan_task(tasks: list[dict], plan: Any) -> dict | None:
    """The planned task whose plan this is, if any."""
    return next(
        (
            task
            for task in tasks
            if task.get("stage") == "planned" and task.get("plan_id") == plan.id
        ),
        None,
    )


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
    approved = _approved_task(data, tasks)
    if approved is not None:
        cap = int(worker["max_runs"])
        if cap and runs_today(worker, now) >= cap:
            return Decision("wait", f"waiting: {cap} runs today, the daily cap")
        preferred = normalize_implementation_agent(data.get("implementation_agent"))
        tool, note = pick_tool(preferred, limits, worker, now)
        if tool is None:
            return Decision("wait", note)
        return Decision(
            "run", f"running {approved['title']} with {tool}{note}", approved, tool
        )
    plan = data.get("pending_plan")
    if plan is not None:
        title = getattr(plan, "feature", "the plan")
        own = _plan_task(tasks, plan)
        if own is not None and own.get("autopilot") and not own.get("autopilot_stuck"):
            if _needs_split(plan):
                return Decision("split", f"splitting {own['title']} (autopilot)", own)
            return Decision("approve", f"approving {own['title']} (autopilot)", own)
        return Decision("wait", f"waiting for your approval: {title}")
    if data.get("pending_implementation") or data.get("pending_bugfix_clarification"):
        return Decision("wait", "waiting: other work is pending")
    todo = next(
        (
            task
            for task in tasks
            if task.get("stage") == "todo"
            and not task.get("worker_failed")
            and waiting_for(task, data) is None
        ),
        None,
    )
    if todo is None:
        return Decision("wait", "idle: no To do tasks")
    preferred = normalize_planning_agent(data.get("planning_agent"))
    tool, note = pick_tool(preferred, limits, worker, now)
    if tool is None:
        return Decision("wait", note)
    return Decision("plan", f"planning {todo['title']} with {tool}{note}", todo, tool)


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


def record(worker: dict, current: dict, outcome: str, title: str, now: float) -> None:
    """Remember how one step went, for the dashboard's stats (last 7 days)."""
    entry = {
        "at": now,
        "phase": current.get("phase"),
        "tool": current.get("tool"),
        "outcome": outcome,
        "title": str(title)[:120],
    }
    history = [
        item
        for item in worker.get("history", [])
        if isinstance(item, dict) and now - item.get("at", 0) < HISTORY_DAYS * DAY
    ]
    worker["history"] = (history + [entry])[-HISTORY_LIMIT:]


def stats(worker: dict, now: float) -> list[dict]:
    """Per day, newest first: steps by outcome and by tool, for the last 7 days."""
    days: dict[str, dict] = {}
    for offset in range(HISTORY_DAYS):
        day = time.strftime("%Y-%m-%d", time.localtime(now - offset * DAY))
        days[day] = {"day": day, **dict.fromkeys(OUTCOMES, 0), "tools": {}}
    for item in worker.get("history", []):
        if not isinstance(item, dict) or item.get("outcome") not in OUTCOMES:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(item.get("at", 0)))
        if day not in days:
            continue
        days[day][item["outcome"]] += 1
        tool = item.get("tool")
        if tool in TOOLS:
            days[day]["tools"][tool] = days[day]["tools"].get(tool, 0) + 1
    return list(days.values())


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
        "current_phase": (merged.get("current") or {}).get("phase"),
        "current_tool": (merged.get("current") or {}).get("tool"),
        "stats": stats(merged, now),
        "recent": [
            item
            for item in (merged.get("history") or [])[-5:][::-1]
            if isinstance(item, dict)
        ],
        "auto_tool": bool(merged.get("auto_tool", True)),
        "review_proposals": int(merged.get("review_proposals", 3)),
        "last_review": (merged.get("review") or {}).get("done"),
        "out_until": {
            tool: until
            for tool, until in (merged.get("out_until") or {}).items()
            if isinstance(until, (int, float)) and until > now
        },
    }
