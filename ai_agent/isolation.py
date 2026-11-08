"""Worker-owned disposable sandbox worktrees for provider implementation runs.

Each implementation run edits a disposable ``git worktree`` under WORK_ROOT
instead of the production checkout. The worktree (and its per-worktree gitdir)
is chowned to WORKER_USER when the agent is root, so the provider CLI — launched
with that uid/gid — can write files and its own index while the production
checkout and its refs stay root-owned. After the provider exits and its process
group is verified dead, the sandbox diff is re-applied to the production
checkout, and the sandbox is removed.

The provider's cwd is always the sandbox, never ``active_project().repo_path``;
``run_implementation_agent`` reads ``current_sandbox`` for that cwd so callers
that stub it out (tests) never see the isolation wiring.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import subprocess
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ai_agent.config import WORK_ROOT, WORKER_GROUP, WORKER_HOME, WORKER_USER
from ai_agent.shell import run

logger = logging.getLogger(__name__)

_active_sandbox: ContextVar["Sandbox | None"] = ContextVar(
    "isolation_sandbox", default=None
)


@dataclass(frozen=True)
class Sandbox:
    """A disposable worktree and the commit its diff is measured against."""

    path: Path
    baseline: str


def sandbox_name(task_id: str, branch: str) -> str:
    """A stable, collision-resistant directory name for one task/branch."""
    digest = hashlib.sha256(f"{task_id}\0{branch}".encode()).hexdigest()[:20]
    return f"ai-run-{digest}"


def current_sandbox() -> Sandbox | None:
    """The active sandbox for the current provider run, or None."""
    return _active_sandbox.get()


def _uid(name: str) -> int:
    import pwd

    return pwd.getpwnam(name).pw_uid


def _gid(name: str) -> int:
    import grp

    return grp.getgrnam(name).gr_gid


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    """Recursively chown a worktree without following symlinks."""
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except FileNotFoundError:
        return
    for root, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            try:
                os.chown(Path(root) / name, uid, gid, follow_symlinks=False)
            except (FileNotFoundError, PermissionError):
                continue


def _linked_gitdir(path: Path) -> Path:
    """The per-worktree gitdir referenced by a linked worktree's ``.git`` file."""
    marker = (path / ".git").read_text().strip()
    if marker.startswith("gitdir:"):
        value = marker.split(":", 1)[1].strip()
        gitdir = Path(value)
        if not gitdir.is_absolute():
            gitdir = path / gitdir
        return gitdir.resolve()
    return (path / ".git").resolve()


def _prepare_ownership(path: Path) -> None:
    """Give the worker its HOME and, when root, ownership of the sandbox."""
    WORKER_HOME.mkdir(parents=True, exist_ok=True)
    if os.geteuid() != 0:
        return
    try:
        uid = _uid(WORKER_USER)
        gid = _gid(WORKER_GROUP)
    except (KeyError, ImportError):
        # The Ops user does not exist yet (or this is a dev machine); the
        # sandbox stays root-owned and the provider drop will surface it.
        logger.warning(
            "Worker user %s:%s not found; sandbox stays root-owned.",
            WORKER_USER,
            WORKER_GROUP,
        )
        return
    _chown_tree(path, uid, gid)
    _chown_tree(_linked_gitdir(path), uid, gid)
    _chown_tree(WORKER_HOME, uid, gid)


def create_sandbox(repo: Path, task_id: str, branch: str) -> Sandbox:
    """Add a detached worker-owned worktree at the production HEAD."""
    head = run(["git", "rev-parse", "HEAD"], cwd=repo).output.strip()
    name = sandbox_name(task_id, branch)
    path = WORK_ROOT / name
    remove_sandbox(repo, Sandbox(path=path, baseline=head))
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "--detach", str(path), head], cwd=repo)
    _prepare_ownership(path)
    return Sandbox(path=path, baseline=head)


def seed_sandbox(repo: Path, sandbox: Sandbox) -> Sandbox:
    """Copy the production working tree into the sandbox and fix a baseline.

    A resume run has verified saved edits already in the production working
    tree. The provider must continue from those, so they are applied to the
    sandbox and committed as a baseline; the post-run diff is then only the
    provider's *new* edits, which re-apply cleanly on top of the saved ones.
    """
    run(["git", "add", "-N", "."], cwd=repo)
    patch = run(["git", "diff", "--binary", "--no-ext-diff", "HEAD"], cwd=repo).output
    if not patch.strip():
        return sandbox
    run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=sandbox.path,
        input_text=patch,
    )
    run(["git", "add", "-A"], cwd=sandbox.path)
    run(
        ["git", "commit", "--no-verify", "-q", "-m", "ai-agent sandbox baseline"],
        cwd=sandbox.path,
    )
    baseline = run(["git", "rev-parse", "HEAD"], cwd=sandbox.path).output.strip()
    return Sandbox(path=sandbox.path, baseline=baseline)


def sandbox_patch(sandbox: Sandbox) -> str:
    """The full sandbox diff since its baseline, including binaries and renames."""
    run(["git", "add", "-A"], cwd=sandbox.path)
    return run(
        ["git", "diff", "--binary", "--no-ext-diff", "--cached", sandbox.baseline],
        cwd=sandbox.path,
    ).output


def apply_sandbox(repo: Path, sandbox: Sandbox) -> None:
    """Re-apply the provider's sandbox edits to the production working tree."""
    patch = sandbox_patch(sandbox)
    if not patch.strip():
        return
    run(["git", "apply", "--whitespace=nowarn", "-"], cwd=repo, input_text=patch)
    _sync_gitlinks(repo, sandbox.path)


def _submodule_gitlinks(checkout: Path) -> dict[str, str]:
    """Staged gitlink entries (``path -> sha``) in one checkout's index."""
    gitlinks: dict[str, str] = {}
    for line in run(["git", "ls-files", "-s"], cwd=checkout).output.splitlines():
        meta, _, path = line.partition("\t")
        if not path:
            continue
        fields = meta.split()
        if len(fields) >= 2 and fields[0] == "160000":
            gitlinks[path] = fields[1]
    return gitlinks


def _sync_gitlinks(repo: Path, checkout: Path) -> None:
    """Copy the provider's submodule pointers into the production index.

    ``git apply`` (working-tree only) cannot touch gitlinks — they live in the
    index, not the filesystem — so a submodule pointer change would otherwise
    be silently lost. The sandbox index was fully staged by ``sandbox_patch``,
    so its gitlink entries are the authoritative post-run state.
    """
    for path, sha in _submodule_gitlinks(checkout).items():
        run(
            ["git", "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}"],
            cwd=repo,
        )


def remove_sandbox(repo: Path, sandbox: Sandbox) -> None:
    """Best-effort unregister and delete a disposable worktree."""
    try:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "remove",
                "--force",
                str(sandbox.path),
            ],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "prune"],
            capture_output=True,
            check=False,
        )
    except OSError:
        pass
    shutil.rmtree(sandbox.path, ignore_errors=True)


@contextlib.contextmanager
def sandbox_for(repo: Path, task_id: str, branch: str) -> Iterator[Sandbox]:
    """Create, seed, activate and later remove one disposable provider sandbox."""
    sandbox = create_sandbox(repo, task_id, branch)
    sandbox = seed_sandbox(repo, sandbox)
    token = _active_sandbox.set(sandbox)
    try:
        yield sandbox
    finally:
        _active_sandbox.reset(token)
        remove_sandbox(repo, sandbox)
