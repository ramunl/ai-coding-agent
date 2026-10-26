"""Serialize repository runs and preserve failures without replaying their edits."""

import asyncio
import fcntl
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import ParamSpec, TextIO, TypeVar
from uuid import uuid4

from ai_agent.projects import active_project
from ai_agent.shell import run

P = ParamSpec("P")
R = TypeVar("R")


@dataclass
class RecoverySession:
    """State shared by the async execution and its worker threads."""

    preflight: bool = False
    lock: TextIO | None = None
    blocked: Path | None = None
    references: list[str] = field(default_factory=list)


_session = ContextVar("git_recovery_session", default=None)


@contextmanager
def recovery_session() -> Iterator[RecoverySession]:
    """Keep the repository lease until publication and cleanup finish."""
    state = RecoverySession()
    token = _session.set(state)
    try:
        yield state
    finally:
        if state.lock is not None:
            state.lock.close()
        _session.reset(token)


def serialized_execution(
    function: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Give an async execution a lease shared with asyncio.to_thread calls."""

    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with recovery_session():
            # Cancellation must not release the lease while a provider thread
            # is still editing the checkout. Let execution and cleanup finish.
            task = asyncio.create_task(function(*args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                finally:
                    raise

    return wrapped


def _lease(state: RecoverySession) -> None:
    if state.lock is not None:
        return
    directory = run(["git", "rev-parse", "--git-common-dir"]).output.strip()
    directory = (active_project().repo_path / directory).resolve()
    lock = (directory / "ai-agent-run.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError(
            "Repository already has an active run. Retry later."
        ) from None
    state.lock = lock
    state.blocked = directory / "ai-agent-recovery-blocked"
    if state.blocked.exists():
        raise RuntimeError(
            "Repository recovery is incomplete. Inspect local recovery refs and "
            "checkout, then remove .git/ai-agent-recovery-blocked before retrying."
        )


def require_clean_checkout() -> None:
    """Include untracked paths and submodules regardless of Git ignore settings."""
    status = run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ]
    ).output
    if status.strip():
        raise RuntimeError(
            "Repository checkout is dirty. Commit or preserve owner edits and "
            "clean submodules before retrying; no branch was changed."
        )


def retain_branch(branch: str) -> None:
    """Protect a branch tip before a retry can reset it."""
    refs = run(
        ["git", "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}"]
    ).output.strip()
    if refs:
        reference = f"refs/ai-agent-recovery/{uuid4().hex}/commits"
        run(["git", "update-ref", reference, refs, ""])
        state = _session.get()
        if state:
            state.references.append(reference)


def preserve_changes() -> None:
    """Save HEAD and index/worktree/untracked data; never touch ignored files."""
    state = _session.get()
    reference = f"refs/ai-agent-recovery/{uuid4().hex}"
    run(["git", "update-ref", reference + "/commits", "HEAD", ""])
    if state:
        state.references.append(reference + "/commits")
    # Submodule worktrees cannot be preserved by the superproject stash.
    dirty = run(
        [
            "git",
            "submodule",
            "foreach",
            "--quiet",
            "--recursive",
            "git status --porcelain --untracked-files=all --ignore-submodules=none",
        ]
    )
    if dirty.output.strip():
        raise RuntimeError("Dirty submodule: preserve its changes manually first.")
    status = run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ]
    ).output
    if status.strip():
        # Only intent-to-add paths appear as unstaged additions. Removing these
        # empty index entries makes stash -u work without altering staged data.
        intent = run(["git", "diff", "--name-only", "-z", "--diff-filter=A"]).output
        paths = [path for path in intent.split("\0") if path]
        if paths:
            run(["git", "reset", "--", *paths])
        run(["git", "stash", "push", "--include-untracked", "-m", reference])
        message = run(["git", "log", "-1", "--format=%s", "refs/stash"]).output
        if reference not in message:
            raise RuntimeError("Could not verify the recovery stash.")
        run(["git", "update-ref", reference + "/stash", "refs/stash", ""])
        if state:
            state.references.append(reference + "/stash")
    require_clean_checkout()


def cleanup_recovery() -> bool:
    """Preserve only edits belonging to a run that passed preflight."""
    state = _session.get()
    if state is None or not state.preflight:
        return False
    try:
        preserve_changes()
    except Exception:
        if state.blocked:
            state.blocked.write_text(
                "Inspect recovery refs and checkout before retrying.\n"
            )
        raise RuntimeError(
            "Cleanup failed; checkout was not switched. Inspect local recovery "
            "refs, preserve remaining edits manually, and resolve the recovery block."
        ) from None
    return True


def recovery_details() -> str:
    """Return only generated references, never filenames or command output."""
    state = _session.get()
    if state and state.references:
        return "Local recovery (never automatically reapplied):\n" + "\n".join(
            state.references
        )
    return ""


def guarded_run(function: Callable[P, R]) -> Callable[P, R]:
    """Check cleanliness before any branch mutation, including direct callers."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        if _session.get() is None:
            with recovery_session():
                try:
                    return wrapped(*args, **kwargs)
                except BaseException as error:
                    try:
                        cleanup_recovery()
                    except Exception as cleanup_error:
                        error.add_note(str(cleanup_error))
                    error.add_note(recovery_details())
                    raise
        state = _session.get()
        _lease(state)
        require_clean_checkout()
        state.preflight = True
        return function(*args, **kwargs)

    return wrapped
