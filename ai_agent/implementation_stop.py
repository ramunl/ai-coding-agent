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
    """Register only a task-bound implementation, never a planning process."""
    if not task_id:
        return None
    run = ProviderRun(task_id, branch)
    with _lock:
        if task_id in _runs:
            raise RuntimeError("Task already has an active provider")
        _runs[task_id] = run
    return run


def unregister(run: ProviderRun | None) -> None:
    """An older completion cannot unregister a newer provider."""
    if run is not None:
        with _lock:
            if _runs.get(run.task_id) is run:
                del _runs[run.task_id]


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
