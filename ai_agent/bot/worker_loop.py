"""The autonomous worker's loop (switches and /worker: worker_commands).

Every TICK_SECONDS the worker looks at the agent (ai_agent.worker.decide) and
carries out the decision with the same code the dashboard uses: task_actions
to plan a task, the /confirm handler to run an approved plan. It says in the
owner's chat what it starts and what went wrong.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ai_agent import log_scan
from ai_agent.actions import ActionError
from ai_agent.bot import task_actions, work_actions
from ai_agent.projects import list_projects
from ai_agent.provider_limits import limits_snapshot
from ai_agent.tasks import find_task, set_stage, tasks_of
from ai_agent.worker import (
    MAX_FAILURES,
    TOOLS,
    decide,
    follow_up,
    limit_reset,
    record,
    settings,
)

logger = logging.getLogger(__name__)

TICK_SECONDS = 60


async def _tell(ptb_app: Any, owner_id: int, text: str) -> None:
    try:
        await ptb_app.bot.send_message(chat_id=owner_id, text=f"🤖 Worker: {text}")
    except Exception as error:  # a lost message must not stop the worker
        logger.warning("Worker message not sent: %s", error)


async def _limit_failure(
    ptb_app: Any, owner_id: int, data: Any, worker: dict, current: dict, task: dict
) -> bool:
    """A failure caused by a tool's usage limit: mark the tool out, retry later.

    Not the task's fault, so it neither counts towards switching the worker
    off nor marks the task as failed; a stopped run goes back to To do.
    """
    error = data.get("last_work_error") or {}
    recent = error.get("at", 0) >= current.get("started_at", 0)
    text = error.get("text", "") if recent else ""
    now = time.time()
    until = limit_reset(text or str(task.get("note") or ""), now)
    tool = current.get("tool")
    if until is None or tool is None:
        return False
    worker["out_until"][tool] = until
    if task.get("stage") in ("stopped", "ended"):
        set_stage(task, "todo", f"{tool} hit its usage limit; will retry")
    until_text = time.strftime("%H:%M", time.localtime(until))
    others = [name for name in TOOLS if name != tool]
    plan_b = f"; using {others[0]} meanwhile" if worker.get("auto_tool", True) else ""
    await _tell(
        ptb_app, owner_id, f"{tool} hit its usage limit until {until_text}{plan_b}."
    )
    return True


async def _settle(
    ptb_app: Any, owner_id: int, data: Any, worker: dict, now: float
) -> None:
    """Record how the current task went; switch off after repeated failures."""
    current = worker.get("current")
    task = find_task(data, current["task"]) if current else None
    outcome = follow_up(worker, task)
    if outcome is None:
        return
    worker["current"] = None
    if outcome == "gone":
        return
    if outcome == "ok":
        worker["failures"] = 0
        done = "planned" if current.get("phase") == "planning" else "pr"
        record(worker, current, done, task["title"], now)
        return
    if await _limit_failure(ptb_app, owner_id, data, worker, current, task):
        record(worker, current, "limit", task["title"], now)
        return
    record(worker, current, "failed", task["title"], now)
    worker["failures"] = int(worker.get("failures", 0)) + 1
    task["worker_failed"] = True  # not picked again until started by hand
    await _tell(ptb_app, owner_id, f"“{task['title']}” failed ({task.get('note')}).")
    if worker["failures"] >= MAX_FAILURES:
        worker["enabled"] = False
        await _tell(
            ptb_app,
            owner_id,
            f"switched off after {MAX_FAILURES} failed tasks in a row. "
            "Check the chat above, then /worker on.",
        )


async def tick(ptb_app: Any, owner_id: int, now: float | None = None) -> str:
    """One step of the worker; returns its status line."""
    now = time.time() if now is None else now
    data = ptb_app.user_data[owner_id]
    worker = settings(data)
    await _settle(ptb_app, owner_id, data, worker, now)
    if work_actions._starting:
        decision_status = "waiting: the agent is planning"
        data["worker_status"] = decision_status
        return decision_status
    decision = decide(data, tasks_of(data), limits_snapshot(), now)
    data["worker_status"] = decision.status
    is_idle = worker["enabled"] and decision.status.startswith("idle")
    if is_idle and log_scan.is_due(data, now):
        await scan_logs(ptb_app, owner_id, now)
    if decision.action == "wait" or not worker["enabled"]:
        return decision.status
    task = decision.task
    try:
        if decision.action == "plan":
            await task_actions.start(ptb_app, owner_id, task, decision.tool)
            worker["current"] = {
                "task": task["id"],
                "phase": "planning",
                "tool": decision.tool,
                "started_at": now,
            }
            await _tell(
                ptb_app,
                owner_id,
                f"planning “{task['title']}” in {task['repo']} with {decision.tool}. "
                "Approve the plan to let me run it.",
            )
        else:
            # This run only: the queued task keeps the implementer chosen here.
            pending = data.get("pending_implementation")
            if isinstance(pending, dict):
                pending["implementation_agent"] = decision.tool
            await work_actions.run_work_action(ptb_app, owner_id, "confirm_work", {})
            worker["runs"] = [
                at for at in worker.get("runs", []) if now - at < 24 * 3600
            ] + [now]
            worker["current"] = {
                "task": task["id"],
                "phase": "running",
                "tool": decision.tool,
                "started_at": now,
            }
            await _tell(
                ptb_app, owner_id, f"running “{task['title']}” with {decision.tool}."
            )
    except ActionError as error:
        data["worker_status"] = f"waiting: {error}"
    ptb_app.mark_data_for_update_persistence(user_ids=owner_id)
    return data["worker_status"]


async def scan_logs(
    ptb_app: Any, owner_id: int, now: float | None = None
) -> list[dict]:
    """Propose tasks from the server logs and say so in the chat."""
    now = time.time() if now is None else now
    data = ptb_app.user_data[owner_id]
    projects = {project.name for project in await asyncio.to_thread(list_projects)}
    services = [name for name, repo in log_scan.SERVICES.items() if repo in projects]
    journals = await asyncio.to_thread(
        lambda: {name: log_scan.read_journal(name) for name in services}
    )
    proposed = log_scan.scan(data, projects, now, lambda name: journals.get(name, []))
    ptb_app.mark_data_for_update_persistence(user_ids=owner_id)
    if proposed:
        titles = "\n".join(f"• {task['title'][:120]}" for task in proposed)
        await _tell(
            ptb_app,
            owner_id,
            f"proposed {len(proposed)} task(s) from the server logs; accept or "
            f"dismiss them in Tasks:\n{titles}",
        )
    return proposed


async def worker_forever(
    ptb_app: Any, owner_id: int, interval: float = TICK_SECONDS
) -> None:
    """Run the worker until cancelled; a failing tick is logged and retried."""
    while True:
        try:
            await tick(ptb_app, owner_id)
        except Exception as error:
            logger.warning("Worker tick failed (will retry): %s", error)
        await asyncio.sleep(interval)
