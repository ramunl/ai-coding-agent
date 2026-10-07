"""Setup and work requests from the dashboard, delivered as files and run by the agent.

The dashboard never changes this agent's state itself: it drops a request in
INBOX_DIR, and this loop validates it and runs the same code as the Telegram
command (ai_agent.actions for setup; the command handlers themselves for
work on plans and the queue, see ai_agent/bot/work_actions.py). Results go
to a small file the snapshot publishes, so the dashboard shows them, also
across the restart a model switch causes.

Only known actions with well-formed arguments run. A request older than
MAX_AGE_SECONDS is reported as expired instead of run: a model switch tapped
long ago must not happen when the agent comes back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

from ai_agent.actions import (
    ActionError,
    add_repository,
    set_implementer,
    set_planner,
    switch_model,
    use_project,
)
from ai_agent.bot.atomic_file import write_json_atomic

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0
MAX_AGE_SECONDS = 120
KEEP_RESULTS = 20

_ID = re.compile(r"^[0-9a-f]{8,32}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")
_MODEL = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
_AGENTS = ("codex", "claude")
_TASK = re.compile(r"^[0-9]{1,9}$")
# Free text for a plan or bug report: one line, something visible in it.
_TEXT = re.compile(r"^(?=.*\S)[^\x00-\x1f\x7f]{1,4000}$")
_KINDS = ("plan", "implement", "bugfix")
WorkRunner = Callable[[str, dict[str, str]], Awaitable[str]]

# action -> {argument: allowed pattern or values}
ACTIONS: dict[str, dict[str, Any]] = {
    "use_project": {"name": _NAME},
    "add_repository": {"repository": _REPOSITORY},
    "set_planner": {"value": _AGENTS},
    "set_implementer": {"value": _AGENTS},
    "switch_model": {"tool": ("claude",), "model": _MODEL},
    "approve_plan": {},
    "confirm_work": {},
    "cancel_pending": {},
    "remove_queued": {"task": _TASK},
    "start_work": {"kind": _KINDS, "text": _TEXT},
    "discuss_plan": {"text": _TEXT},
}
WORK_ACTIONS = (
    "approve_plan",
    "confirm_work",
    "cancel_pending",
    "remove_queued",
    "start_work",
    "discuss_plan",
)


def validate(request: dict) -> tuple[str, str, dict[str, str]]:
    """Return (id, action, args) or raise ActionError; unknown keys are refused."""
    request_id = request.get("id")
    if not isinstance(request_id, str) or not _ID.match(request_id):
        raise ActionError("Malformed request id.")
    action = request.get("action")
    spec = ACTIONS.get(action) if isinstance(action, str) else None
    if spec is None:
        raise ActionError(f"Unknown action '{action}'.")
    args = request.get("args")
    if not isinstance(args, dict) or set(args) != set(spec):
        raise ActionError(f"{action} expects exactly: {', '.join(sorted(spec))}.")
    for key, allowed in spec.items():
        value = args[key]
        valid = isinstance(value, str) and (
            value in allowed if isinstance(allowed, tuple) else allowed.match(value)
        )
        if not valid:
            raise ActionError(f"Invalid {key} for {action}.")
    return request_id, action, args


async def execute(
    action: str,
    args: dict[str, str],
    user_data: MutableMapping,
    rename_bot: Callable[[str], Awaitable[None]] | None = None,
    work: WorkRunner | None = None,
) -> str:
    """Run one validated action through the shared core; return its message."""
    if action in WORK_ACTIONS:
        if work is None:
            raise ActionError("Work actions are not available here.")
        return await work(action, args)
    if action == "use_project":
        project = await use_project(args["name"], user_data)
        if rename_bot:
            await rename_bot(project.name)
        return f"Active project: {project.name} ({project.github_repository})"
    if action == "add_repository":
        _project, summary = await add_repository(args["repository"])
        return summary
    if action == "set_planner":
        return set_planner(args["value"], user_data)
    if action == "set_implementer":
        return set_implementer(args["value"], user_data)
    return await switch_model(args["tool"], args["model"])


class ResultLog:
    """The last results, kept in a file so they survive a restart."""

    def __init__(self, path: Path) -> None:
        """Load earlier results from path, if any."""
        self.path = path
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.results: list[dict] = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError):
            self.results = []

    def record(self, request_id: str, action: str, status: str, message: str) -> None:
        """Add or replace the result for request_id, newest first, and save."""
        entry = {
            "id": request_id,
            "action": action,
            "status": status,
            "message": message,
            "at": time.time(),
        }
        others = [result for result in self.results if result.get("id") != request_id]
        ordered = [entry, *others]
        self.results = ordered[:KEEP_RESULTS]
        # Keep the last model switch even after unrelated actions rotate out.
        last_switch = next(
            (item for item in ordered if item.get("action") == "switch_model"), None
        )
        if last_switch and last_switch not in self.results:
            self.results[-1] = last_switch
        write_json_atomic(self.path, self.results)


def _claim(path: Path) -> Path | None:
    """Rename a request before handling it, so it can never run twice."""
    working = path.with_suffix(".working")
    try:
        path.rename(working)
    except OSError:
        return None
    return working


async def process_inbox(
    inbox: Path,
    log: ResultLog,
    user_data: MutableMapping,
    rename_bot: Callable[[str], Awaitable[None]] | None = None,
    now: float | None = None,
    work: WorkRunner | None = None,
) -> int:
    """Handle every waiting request, oldest first; return how many."""
    handled = 0
    for path in sorted(inbox.glob("*.json"), key=lambda item: item.stat().st_mtime):
        working = _claim(path)
        if working is None:
            continue
        handled += 1
        try:
            request = json.loads(working.read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            log.record(working.stem, "unknown", "rejected", "Unreadable request.")
            working.unlink(missing_ok=True)
            continue
        try:
            request_id, action, args = validate(request)
        except ActionError as error:
            safe_id = request.get("id") if isinstance(request.get("id"), str) else ""
            log.record(safe_id[:32] or working.stem, "unknown", "rejected", str(error))
            working.unlink(missing_ok=True)
            continue
        age = (time.time() if now is None else now) - float(
            request.get("requested_at") or 0
        )
        if age > MAX_AGE_SECONDS:
            log.record(
                request_id, action, "expired", "Not run: the request is too old."
            )
            working.unlink(missing_ok=True)
            continue
        log.record(request_id, action, "running", "Working…")
        try:
            message = await execute(action, args, user_data, rename_bot, work)
            log.record(request_id, action, "done", message)
        except ActionError as error:
            log.record(request_id, action, "failed", str(error))
        except Exception as error:  # never let one request stop the inbox
            logger.exception("Inbox action %s failed", action)
            log.record(request_id, action, "failed", f"Unexpected error: {error}")
        finally:
            working.unlink(missing_ok=True)
    return handled


async def inbox_forever(
    ptb_app: Any, owner_id: int, inbox: Path, results_file: Path
) -> None:
    """Poll the inbox until cancelled; a failing pass is logged and retried."""
    inbox.mkdir(parents=True, exist_ok=True)
    log = ResultLog(results_file)

    async def rename_bot(project_name: str) -> None:
        try:
            await ptb_app.bot.set_my_name(name=f"Coding AI Agent - {project_name}"[:64])
        except Exception as error:  # cosmetic; Telegram rate-limits renames
            logger.info("Could not update bot name (ignored): %s", error)

    # Imported here: the handlers pull in the whole bot, which imports this
    # module's caller.
    work: WorkRunner | None = None
    try:
        from ai_agent.bot.work_actions import run_work_action
    except ImportError as error:  # setup requests must keep working regardless
        logger.warning("Work actions unavailable: %s", error)
    else:

        async def work(action: str, args: dict[str, str]) -> str:
            return await run_work_action(ptb_app, owner_id, action, args)

    while True:
        try:
            handled = await process_inbox(
                inbox, log, ptb_app.user_data[owner_id], rename_bot, work=work
            )
            if handled:
                # PTB saves state only for users it just handled an update for;
                # these changes came from the inbox, so mark them explicitly or
                # they would be lost on the next restart.
                ptb_app.mark_data_for_update_persistence(user_ids=owner_id)
        except Exception as error:
            logger.warning("Inbox pass failed (will retry): %s", error)
        await asyncio.sleep(POLL_SECONDS)
