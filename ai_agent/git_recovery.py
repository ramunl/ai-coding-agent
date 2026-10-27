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
    # Leftovers found before this run started and saved aside (set_aside).
    set_aside: list[str] = field(default_factory=list)
    set_aside_refs: list[str] = field(default_factory=list)


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
        try:
            _session.reset(token)
        except ValueError:
            # Closed in another context (the coroutine is finalized while the
            # process shuts down mid-run): clear it instead of crashing here.
            _session.set(None)


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


SHOWN_PATHS = 10


def _status() -> str:
    return run(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ]
    ).output


def dirty_paths(status: str | None = None) -> list[str]:
    """The paths git status reports, as written (renames keep both names)."""
    text = _status() if status is None else status
    return [line[3:].strip() for line in text.splitlines() if len(line) > 3]


def _listed(paths: list[str]) -> str:
    shown = ", ".join(paths[:SHOWN_PATHS])
    more = len(paths) - SHOWN_PATHS
    return shown + (f" and {more} more" if more > 0 else "")


def require_clean_checkout() -> None:
    """Include untracked paths and submodules regardless of Git ignore settings."""
    status = _status()
    if status.strip():
        raise RuntimeError(
            "Repository checkout is dirty: "
            + _listed(dirty_paths(status))
            + ". Commit or preserve owner edits and clean submodules before "
            "retrying; no branch was changed."
        )


def _submodules_dirty_inside() -> list[str]:
    """Submodules with their own uncommitted edits (never touched here)."""
    output = run(
        [
            "git",
            "submodule",
            "foreach",
            "--quiet",
            "--recursive",
            'test -z "$(git status --porcelain --untracked-files=all)" '
            '|| echo "$displaypath"',
        ]
    ).output
    return [line.strip() for line in output.splitlines() if line.strip()]


def set_aside_leftovers() -> None:
    """Save what an earlier run (or a restart mid-run) left in the checkout.

    A run that was killed (a deploy restarts the agent) never cleans up, and
    then every later run would refuse the dirty checkout. The leftovers are
    saved to a recovery ref exactly as a failed run's are (never reapplied,
    never published), so this run starts clean and the owner can restore them.
    A submodule only pointing at another commit is checked out again; one with
    edits of its own stops the run, since those cannot be saved here.
    """
    status = _status()
    if not status.strip():
        return
    paths = dirty_paths(status)
    inside = _submodules_dirty_inside()
    if inside:
        raise RuntimeError(
            "Repository checkout is dirty and a submodule has edits of its own: "
            + _listed(inside)
            + ". Save or discard them on the server, then retry; no branch was "
            "changed."
        )
    run(["git", "submodule", "update", "--init", "--recursive"])
    state = _session.get()
    before = list(state.references) if state else []
    if _status().strip():
        preserve_changes()
    if state:
        state.set_aside = paths
        state.set_aside_refs = [ref for ref in state.references if ref not in before]


def set_aside_notice() -> str:
    """What was set aside before this run started, for the owner; or ''."""
    state = _session.get()
    if not state or not state.set_aside:
        return ""
    stash = next((ref for ref in state.set_aside_refs if ref.endswith("/stash")), "")
    restore = (
        f"\nTo get them back on the server: git stash apply {stash}" if stash else ""
    )
    return (
        "The checkout had leftovers from an earlier run (or a restart mid-run): "
        + _listed(state.set_aside)
        + ". They were saved aside, not included in this run."
        + restore
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
        set_aside_leftovers()
        require_clean_checkout()
        state.preflight = True
        return function(*args, **kwargs)

    return wrapped
