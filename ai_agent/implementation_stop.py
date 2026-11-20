"""Attempt-bound cooperative stop requests for provider subprocesses."""

from dataclasses import dataclass, field
from threading import Event, Lock
from uuid import uuid4


class ImplementationStoppedError(RuntimeError):
    """The owner stopped the provider; normal recovery must still finish."""


@dataclass
class ProviderRun:
    """One in-memory capability, never reconstructed from a persisted PID."""

    task_id: str
    branch: str
    token: str = field(default_factory=lambda: uuid4().hex)
    stop: Event = field(default_factory=Event)


_lock = Lock()
_runs: dict[str, ProviderRun] = {}


def register(task_id: str, branch: str) -> ProviderRun | None:
    """Register a bound implementation, including runs without an owner task."""
    if not branch:
        return None
    run = ProviderRun(task_id, branch)
    with _lock:
        if task_id in _runs:
            raise RuntimeError("Task already has an active provider")
        _runs[task_id] = run
    return run


def unregister(run: ProviderRun | None) -> bool:
    """Retire the capability and return whether stop won the completion race."""
    if run is not None:
        with _lock:
            if _runs.get(run.task_id) is run:
                del _runs[run.task_id]
            return run.stop.is_set()
    return False


def status(task_id: str, branch: str) -> dict:
    """Expose an opaque attempt capability and the pending stop state."""
    with _lock:
        run = _runs.get(task_id)
        if run is None or run.branch != branch:
            return {"can_stop": False, "stopping": False}
        return {
            "can_stop": True,
            "stopping": run.stop.is_set(),
            "stop_token": run.token,
        }


def request(task_id: str, branch: str, token: str) -> None:
    """Reject stale UI requests rather than stopping a subsequent attempt."""
    with _lock:
        run = _runs.get(task_id)
        if run is None or run.branch != branch or run.token != token:
            raise ValueError(
                "That implementation is no longer running; refresh the task"
            )
        run.stop.set()


def current_status(active: dict | None) -> dict:
    """Coding-window capability; only a live provider can be stopped.

    Recovery remains visibly stopping after the provider capability is retired.
    Publication, planning and CI waiting never acquire a capability.
    """
    active = active or {}
    with _lock:
        for run in _runs.values():
            if run.branch == active.get("branch"):
                return {
                    "can_stop": True,
                    "stopping": run.stop.is_set(),
                    "stop_token": run.token,
                    "queue_policy": "pause",
                }
    return {
        "can_stop": False,
        "stopping": str(active.get("phase", "")).startswith("Stopping"),
        "reason": "Stop is available only while an implementation provider is running.",
        "queue_policy": "pause",
    }


def request_current(active: dict | None, token: str) -> str:
    """Validate the live branch and opaque attempt token atomically.

    Return the optional task ID so callers preserve task stop bookkeeping.
    Repeated requests for the same live capability are harmless.
    """
    with _lock:
        for run in _runs.values():
            if run.branch == (active or {}).get("branch") and run.token == token:
                run.stop.set()
                return run.task_id
    raise ValueError("That implementation is no longer running; refresh Coding.")
