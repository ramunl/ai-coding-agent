"""Bounded, secret-free task progress and owner-only five-minute updates.

The live projection reads only the transient ``active_execution`` record kept by
:func:`ai_agent.bot.state.set_active_execution`. It is bound to one task and one
attempt and exposes canonical stage labels and whole-second ages — never
provider output, prompts, paths, or credentials.

The durable journal (``ai_agent.context_attempts.Attempt``) records
``started_at`` and per-event ``observed_at`` wall-clock timestamps. A journal
left behind by a previous process is not a live heartbeat: an unfinished
journal projects an ``unknown`` stage instead of inferring completion.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

PROGRESS_INTERVAL_SECONDS = 300.0

_STAGE_VALUES = frozenset(
    {
        "checkout",
        "implement",
        "push",
        "pr",
        "ci",
        "repair",
        "stopping",
        "working",
        "unknown",
    }
)

_PHASE_PREFIXES = (
    ("checking github", "checkout"),
    ("running ", "implement"),
    ("committing", "push"),
    ("pushing", "push"),
    ("opening github", "pr"),
    ("polling", "ci"),
    ("repairing", "repair"),
    ("stopping", "stopping"),
)

_JOURNAL_STAGES = {
    "checkout": "checkout",
    "diff": "implement",
    "implementation_intent": "implement",
    "commit": "push",
    "commit_intent": "push",
    "push": "push",
    "push_intent": "push",
    "pr": "pr",
    "pr_intent": "pr",
    "ci": "ci",
    "cancellation_requested": "stopping",
}


def stage_label(value: object) -> str:
    """Map a free-text phase to a canonical, bounded stage label."""
    text = str(value or "").strip().lower()
    for prefix, label in _PHASE_PREFIXES:
        if text.startswith(prefix):
            return label
    return "working"


def journal_stage_label(value: object) -> str | None:
    """Map a durable journal stage to a canonical label; unknown stages are None."""
    if not isinstance(value, str):
        return None
    return _JOURNAL_STAGES.get(value)


def _timestamp(value: object) -> float | None:
    """Accept only non-negative numeric timestamps."""
    if type(value) in (int, float) and value >= 0:
        return float(value)
    return None


def _age(started: object, now: float) -> int | None:
    """Whole seconds since ``started``, or None for missing or future values."""
    value = _timestamp(started)
    if value is None or value > now:
        return None
    return max(0, int(now - value))


def _identifier(value: object) -> str | None:
    """Keep only bounded, benign identifiers; reject paths, spaces and prose."""
    if not isinstance(value, str):
        return None
    identifier = value.strip()
    if not identifier or len(identifier) > 128:
        return None
    if any(not (character.isalnum() or character in "-_") for character in identifier):
        return None
    return identifier


def progress(active: object, *, now: float | None = None) -> dict | None:
    """Project the live execution into a bounded, secret-free progress object.

    Returns ``None`` when no run is active in this process — including after a
    restart, because ``active_execution`` is transient and never restored. An
    interrupted run is therefore never reported as still running or completed
    from a stale heartbeat.
    """
    if not isinstance(active, dict):
        return None
    now = time.time() if now is None else now
    started = _timestamp(active.get("started_at"))
    stage = active.get("stage") or stage_label(active.get("phase"))
    return {
        "task_id": _identifier(active.get("task_id")),
        "attempt_id": _identifier(active.get("attempt_id")),
        "stage": stage if stage in _STAGE_VALUES else "unknown",
        "elapsed": _age(started, now),
        "last_activity": _age(active.get("last_activity"), now),
    }


def restart_progress() -> dict:
    """The unknown marker for a run that died with the previous process."""
    return {
        "task_id": None,
        "attempt_id": None,
        "stage": "unknown",
        "elapsed": None,
        "last_activity": None,
    }


def _latest_observed(state: dict) -> float | None:
    """The newest event timestamp in a journal, ignoring legacy events."""
    events = state.get("events")
    latest = state.get("latest")
    if not isinstance(events, dict) or not isinstance(latest, dict):
        return None
    observed: list[float] = []
    for key in latest.values():
        event = events.get(key) if isinstance(key, str) else None
        if isinstance(event, dict):
            value = _timestamp(event.get("observed_at"))
            if value is not None:
                observed.append(value)
    return max(observed) if observed else None


def journal_progress(state: object, *, now: float | None = None) -> dict | None:
    """Project an attempt journal conservatively after a restart.

    Terminal attempts have no live progress. An unfinished journal left by a
    previous process is never treated as a live heartbeat, so its last
    checkpoint cannot establish a current stage: the stage is ``unknown`` while
    the durable start and observation ages are still reported.
    """
    if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
        return None
    if state.get("terminal"):
        return None
    spec = state.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    task_id = _identifier(spec.get("task_id"))
    attempt_id = _identifier(state.get("attempt_id"))
    if task_id is None and attempt_id is None:
        return None
    now = time.time() if now is None else now
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "stage": "unknown",
        "elapsed": _age(state.get("started_at"), now),
        "last_activity": _age(_latest_observed(state), now),
    }


def render_progress_message(view: dict, *, quiet: bool = False) -> str:
    """Owner-only progress text; stage labels and ages only, never payloads."""
    parts = [f"🔄 Progress: {view.get('stage', 'unknown')}"]
    if view.get("elapsed") is not None:
        parts.append(f"elapsed {view['elapsed']}s")
    if view.get("last_activity") is not None:
        parts.append(f"last activity {view['last_activity']}s ago")
    if quiet:
        parts.append("(no new activity)")
    attempt = view.get("attempt_id")
    if attempt:
        parts.append(f"attempt {attempt[:8]}")
    return " ".join(parts)


_last_update: dict[str, dict] = {}


def clear_progress_state() -> None:
    """Forget finished attempts so a stale id cannot suppress a new run."""
    _last_update.clear()


async def notify_once(
    ptb_app: Any, owner_id: int, *, now: float | None = None
) -> int:
    """Send one progress update when a run is active and its five-minute slot is due.

    Returns the number of messages sent (0 or 1). A missing active record means
    the run ended or the process restarted, so finished attempts are forgotten
    and nothing is sent — this never duplicates the completion/failure messages
    the execution flow already posts. Updates are owner-only and never call a
    provider, so they cannot trigger extra AI work.
    """
    now = time.time() if now is None else now
    data = ptb_app.user_data.get(owner_id, {})
    active = data.get("active_execution")
    view = progress(active, now=now)
    if view is None:
        clear_progress_state()
        return 0
    attempt_id = view.get("attempt_id")
    if not attempt_id:
        return 0
    previous = _last_update.get(attempt_id)
    if (
        previous is not None
        and now - previous["notified_at"] < PROGRESS_INTERVAL_SECONDS
    ):
        return 0
    last_value = _timestamp(
        active.get("last_activity") if isinstance(active, dict) else None
    )
    quiet = (
        previous is not None
        and previous.get("last_activity") is not None
        and last_value is not None
        and last_value <= previous["last_activity"] + 0.5
    )
    _last_update[attempt_id] = {
        "notified_at": now,
        "last_activity": last_value,
    }
    try:
        await ptb_app.bot.send_message(
            chat_id=owner_id, text=render_progress_message(view, quiet=quiet)
        )
    except Exception as error:  # a lost update must not stop the notifier
        logger.warning("Progress update not sent: %s", error)
        _last_update.pop(attempt_id, None)
        return 0
    return 1


async def progress_notifier_forever(
    ptb_app: Any,
    owner_id: int,
    interval: float = PROGRESS_INTERVAL_SECONDS,
) -> None:
    """Run the progress notifier until cancelled; a failing tick is retried."""
    while True:
        try:
            await notify_once(ptb_app, owner_id)
        except Exception as error:
            logger.warning("Progress notifier failed (will retry): %s", error)
        await asyncio.sleep(interval)
