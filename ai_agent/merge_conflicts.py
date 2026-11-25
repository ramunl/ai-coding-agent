"""Resolve a pinned PR's merge conflicts inside a disposable local clone.

The prerequisite (``ai_agent.merge_conflict``) pins the PR head and base SHAs
and makes both available as objects in the production repository. This module
checks the head out in a disposable clone of that repository, merges the base
with ``--no-commit --no-ff``, hands conflict data to a resolver, validates the
staged result, and returns the validated tree and parent ids. The production
checkout's branch, HEAD, index and working tree are never modified; the
validated tree is copied back into the production object store so the
publishing milestone can create the merge commit, and the clone is removed on
every exit path.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_agent.merge_conflict_stages import Conflict, parse_index, parse_unmerged

UP_TO_DATE = "up_to_date"
CLEAN = "clean"
RESOLVED = "resolved"

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_CONFLICT_MARKER = re.compile(r"^(?:<<<<<<<|>>>>>>>|=======\r?$)", re.MULTILINE)


class MergeConflictResolutionError(RuntimeError):
    """A merge that failed or produced an invalid resolution."""


class Resolver(Protocol):
    """Resolve conflicts inside the isolated checkout and stage the result."""

    def __call__(self, checkout: Path, conflicts: list[Conflict]) -> None:
        """Edit and stage a resolution for every conflict, or raise."""


@dataclass(frozen=True)
class MergeOutcome:
    """The validated result of a merge, ready for the publishing milestone."""

    state: str
    tree: str | None
    parents: tuple[str, ...]


@dataclass(frozen=True)
class MergeCheckout:
    """A disposable clone handed to the resolver and removed after its scope."""

    path: Path
    head_sha: str
    base_sha: str


def _git(cwd: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git (optionally in a checkout) and return the raw result."""
    command = ["git", *args] if cwd is None else ["git", "-C", str(cwd), *args]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _must(cwd: Path | None, *args: str) -> str:
    """Run git, raising a merge-conflict error on failure, and return stdout."""
    result = _git(cwd, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MergeConflictResolutionError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _require_commit(source: Path, sha: str) -> None:
    """Reject ids that are not a locally available commit object."""
    if not _COMMIT.fullmatch(sha):
        raise MergeConflictResolutionError(f"Invalid commit id: {sha!r}")
    if _git(source, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise MergeConflictResolutionError(f"Commit {sha} is not available locally")


def create_checkout(source: Path, head_sha: str, base_sha: str) -> MergeCheckout:
    """Clone the source repository and check out the pinned head."""
    _require_commit(source, head_sha)
    _require_commit(source, base_sha)
    path = Path(tempfile.mkdtemp(prefix="ai-agent-merge-"))
    checkout = MergeCheckout(path=path, head_sha=head_sha, base_sha=base_sha)
    try:
        _must(
            None,
            "clone",
            "--quiet",
            "--no-checkout",
            "--shared",
            str(source),
            str(path),
        )
        _must(path, "checkout", "--quiet", "--detach", head_sha)
    except BaseException:
        teardown(checkout)
        raise
    return checkout


def teardown(checkout: MergeCheckout) -> None:
    """Remove the disposable clone; safe to call more than once."""
    shutil.rmtree(checkout.path, ignore_errors=True)


@contextlib.contextmanager
def merge_checkout(
    source: Path, head_sha: str, base_sha: str
) -> Iterator[MergeCheckout]:
    """Yield a disposable clone and remove it after use, failure, or cancel."""
    checkout = create_checkout(source, head_sha, base_sha)
    try:
        yield checkout
    finally:
        teardown(checkout)


def resolve_merge_conflicts(
    source: Path,
    head_sha: str,
    base_sha: str,
    resolver: Resolver,
) -> MergeOutcome:
    """Investigate, resolve, validate, and hand off a pinned merge.

    Returns the validated tree and parent ids for the publishing milestone.
    The disposable clone is removed on every exit path; only the validated
    tree (and any new blobs it needs) is copied back into ``source``'s object
    store.
    """
    with merge_checkout(source, head_sha, base_sha) as checkout:
        investigated = _investigate(checkout)
        if isinstance(investigated, MergeOutcome):
            outcome = investigated
        else:
            outcome = _resolve(checkout, investigated, resolver)
        if outcome.tree is not None:
            _persist_tree(source, checkout, outcome.tree)
        return outcome


def _merge_head(checkout: MergeCheckout) -> str | None:
    result = _git(checkout.path, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return result.stdout.strip() if result.returncode == 0 else None


def _unmerged(checkout: MergeCheckout) -> list[Conflict]:
    result = _git(checkout.path, "ls-files", "--unmerged", "-z")
    if result.returncode != 0:
        raise MergeConflictResolutionError("Could not read the unmerged index")
    return parse_unmerged(result.stdout)


def _stage0_index(checkout: MergeCheckout) -> dict[str, tuple[str, str]]:
    result = _git(checkout.path, "ls-files", "-z", "--stage")
    if result.returncode != 0:
        raise MergeConflictResolutionError("Could not read the index")
    return {
        entry.path: (entry.mode, entry.blob)
        for entry in parse_index(result.stdout)
        if entry.stage == 0
    }


def _merge(checkout: MergeCheckout) -> subprocess.CompletedProcess[str]:
    return _git(
        checkout.path,
        "-c",
        "user.email=merge-conflicts@ai-agent.invalid",
        "-c",
        "user.name=ai-agent",
        "merge",
        "--no-commit",
        "--no-ff",
        "--no-edit",
        checkout.base_sha,
    )


def _investigate(checkout: MergeCheckout) -> MergeOutcome | list[Conflict]:
    """Merge the base into the head and classify the result."""
    merge = _merge(checkout)
    merge_head = _merge_head(checkout)
    unmerged = _unmerged(checkout)

    if merge_head is None and merge.returncode == 0:
        return MergeOutcome(state=UP_TO_DATE, tree=None, parents=(checkout.head_sha,))

    if merge_head == checkout.base_sha and not unmerged and merge.returncode == 0:
        _validate_state(checkout)
        return MergeOutcome(
            state=CLEAN,
            tree=_write_tree(checkout),
            parents=(checkout.head_sha, checkout.base_sha),
        )

    if merge_head == checkout.base_sha and unmerged and merge.returncode != 0:
        return unmerged

    detail = (merge.stderr or merge.stdout).strip()
    raise MergeConflictResolutionError(f"Merge failed: {detail}")


def _resolve(
    checkout: MergeCheckout,
    conflicts: list[Conflict],
    resolver: Resolver,
) -> MergeOutcome:
    """Run the resolver on the conflicts and validate its staged result."""
    before = _stage0_index(checkout)
    resolver(checkout.path, conflicts)
    _validate_resolution(checkout, conflicts, before)
    return MergeOutcome(
        state=RESOLVED,
        tree=_write_tree(checkout),
        parents=(checkout.head_sha, checkout.base_sha),
    )


def _validate_state(checkout: MergeCheckout) -> None:
    """Reject a checkout whose HEAD, MERGE_HEAD, index, or worktree drifted."""
    head = _git(checkout.path, "rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != checkout.head_sha:
        raise MergeConflictResolutionError(
            "Resolver changed HEAD; expected the pinned head"
        )
    if _merge_head(checkout) != checkout.base_sha:
        raise MergeConflictResolutionError("MERGE_HEAD is not the pinned base")
    if _unmerged(checkout):
        raise MergeConflictResolutionError(
            "Unmerged index entries remain after resolution"
        )
    if _git(checkout.path, "diff", "--quiet").returncode != 0:
        raise MergeConflictResolutionError("Working tree does not match the index")
    untracked = _untracked(checkout)
    if untracked:
        raise MergeConflictResolutionError(
            "Unexpected untracked files: " + ", ".join(untracked)
        )


def _validate_resolution(
    checkout: MergeCheckout,
    conflicts: list[Conflict],
    before: dict[str, tuple[str, str]],
) -> None:
    """Validate the resolved state and that only conflict paths changed."""
    _validate_state(checkout)
    conflict_paths = {conflict.path for conflict in conflicts}
    after = _stage0_index(checkout)
    outside_before = {
        path: entry for path, entry in before.items() if path not in conflict_paths
    }
    outside_after = {
        path: entry for path, entry in after.items() if path not in conflict_paths
    }
    if outside_before != outside_after:
        raise MergeConflictResolutionError(
            "Resolver changed staged paths outside the conflict set"
        )
    _reject_conflict_markers(checkout, conflicts)


def _untracked(checkout: MergeCheckout) -> list[str]:
    result = _git(checkout.path, "ls-files", "--others", "--exclude-standard", "-z")
    if result.returncode != 0:
        raise MergeConflictResolutionError("Could not list untracked files")
    return [path for path in result.stdout.split("\0") if path]


def _blob_bytes(checkout: MergeCheckout, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(checkout.path), "cat-file", "blob", f":{path}"],
        capture_output=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise MergeConflictResolutionError(f"Resolved path {path} is not staged")
    return result.stdout


def _reject_conflict_markers(
    checkout: MergeCheckout, conflicts: list[Conflict]
) -> None:
    """Reject resolved text that still carries git conflict markers."""
    for conflict in conflicts:
        content = _blob_bytes(checkout, conflict.path)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _CONFLICT_MARKER.search(text):
            raise MergeConflictResolutionError(
                f"Leftover conflict markers in {conflict.path}"
            )


def _write_tree(checkout: MergeCheckout) -> str:
    return _must(checkout.path, "write-tree").strip()


def _persist_tree(source: Path, checkout: MergeCheckout, tree: str) -> None:
    """Copy the validated tree (and its blobs) into the source object store.

    A ``--shared`` clone reads source objects through an alternate but writes
    new objects only into its own store, so the tree must be fetched back
    before the clone is removed. A temporary ref makes the otherwise
    unreferenced tree fetchable.
    """
    reference = "refs/ai-agent/merge-conflict/result"
    _must(checkout.path, "update-ref", reference, tree)
    try:
        _must(source, "fetch", "--quiet", "--no-tags", str(checkout.path), reference)
    finally:
        _git(checkout.path, "update-ref", "-d", reference)


def run_resolver_command(
    command: list[str],
    checkout: Path,
    conflicts: list[Conflict],
    *,
    timeout: int | None = None,
) -> None:
    """Run a resolver CLI in its own session, reaping it before returning.

    Conflict data is serialized to JSON and passed on stdin, so nothing extra
    is written into the isolated checkout. The resolver runs in a new session
    so its whole process group is killed and reaped on timeout, cancellation,
    or failure before the caller removes the checkout.
    """
    payload = json.dumps(
        [
            {
                "path": conflict.path,
                "stages": {
                    str(number): {"mode": stage.mode, "blob": stage.blob}
                    for number, stage in conflict.stages().items()
                },
            }
            for conflict in conflicts
        ]
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=str(checkout),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError as error:
        raise MergeConflictResolutionError(
            f"Could not start resolver: {error}"
        ) from error
    try:
        stdout, stderr = process.communicate(input=payload, timeout=timeout)
    except BaseException:
        _stop_group(process)
        raise
    finally:
        _stop_group(process)
    if process.returncode != 0:
        detail = (stderr or stdout).strip()
        raise MergeConflictResolutionError(
            f"Resolver failed ({process.returncode}): {detail}"
        )


def _stop_group(process: subprocess.Popen) -> None:
    """Kill and reap the resolver process group; never raises."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
