"""Publish a validated merge resolution as one merge commit on the PR head.

The prerequisite (``ai_agent.merge_conflicts``) produces a validated
``MergeOutcome`` whose tree and parent ids are already available in the source
repository's object store. This module turns that outcome into a single merge
commit whose parents are the pinned PR head and base, then pushes it to the PR
head ref on the remote. Only the head ref is pushed; the base branch is never
updated.

Every guard runs before the atomic single-ref push, so a moved ref, an
unresolved index, or a failed check leaves the remote ref exactly as it was.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

from ai_agent.merge_conflicts import MergeOutcome

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_COMMIT_MESSAGE = "Fix merge conflicts"


class MergeConflictPublishError(RuntimeError):
    """A validated merge resolution that could not be published."""


class VerifyHook(Protocol):
    """Check that a freshly created merge commit matches the outcome."""

    def __call__(
        self, source: Path, sha: str, tree: str, parents: tuple[str, ...]
    ) -> None:
        """Raise if the commit's tree or parents do not match."""


def _git(source: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the source checkout and return the raw result."""
    return subprocess.run(
        ["git", "-C", str(source), *args],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _must(source: Path, *args: str) -> str:
    """Run git, raising a publish error on failure, and return stdout."""
    result = _git(source, *args)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MergeConflictPublishError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _require_commit(source: Path, sha: str) -> None:
    """Reject ids that are not a locally available commit object."""
    if not _COMMIT.fullmatch(sha):
        raise MergeConflictPublishError(f"Invalid commit id: {sha!r}")
    if _git(source, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise MergeConflictPublishError(f"Commit {sha} is not available locally")


def _commit_tree(source: Path, tree: str, parents: tuple[str, ...]) -> str:
    """Create one merge commit from a tree and ordered parents."""
    args = [
        "-c",
        "user.email=merge-conflicts@ai-agent.invalid",
        "-c",
        "user.name=ai-agent",
        "commit-tree",
        tree,
    ]
    for parent in parents:
        args += ["-p", parent]
    args += ["-m", _COMMIT_MESSAGE]
    return _must(source, *args).strip()


def _verify_commit(source: Path, sha: str, tree: str, parents: tuple[str, ...]) -> None:
    """Fail unless the commit's tree and parents match the outcome."""
    actual_tree = _must(source, "rev-parse", f"{sha}^{{tree}}").strip()
    if actual_tree != tree:
        raise MergeConflictPublishError(
            f"Merge commit tree {actual_tree} does not match the validated tree {tree}"
        )
    result = _git(source, "rev-parse", f"{sha}^@")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MergeConflictPublishError(
            f"Could not read the merge commit parents: {detail}"
        )
    actual_parents = tuple(line for line in result.stdout.splitlines() if line)
    if actual_parents != tuple(parents):
        raise MergeConflictPublishError(
            f"Merge commit parents {actual_parents} do not match the validated "
            f"parents {tuple(parents)}"
        )


def _ls_remote_head(source: Path, remote: str, head_ref: str) -> str | None:
    """Return the remote head SHA, or ``None`` if the ref does not exist."""
    result = _git(source, "ls-remote", remote, f"refs/heads/{head_ref}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MergeConflictPublishError(f"Could not read {remote!r}: {detail}")
    lines = result.stdout.splitlines()
    if not lines:
        return None
    return lines[0].split()[0]


def _push_head(source: Path, remote: str, head_ref: str, sha: str) -> None:
    """Push exactly the merge commit to the PR head ref and nothing else."""
    result = _git(source, "push", remote, f"{sha}:refs/heads/{head_ref}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise MergeConflictPublishError(f"Push failed: {detail}")


def publish_merge_commit(
    source: Path,
    remote: str,
    head_ref: str,
    head_sha: str,
    base_sha: str,
    outcome: MergeOutcome,
    *,
    verify: VerifyHook | None = None,
) -> str | None:
    """Publish ``outcome`` as one merge commit on the pinned PR head.

    Returns the new merge commit SHA, or ``None`` for an up-to-date outcome
    (``tree is None``) that needs no commit and no push. The commit is built
    with ``outcome.tree`` and parents ``(head_sha, base_sha)``. A moved remote
    head, an unresolved index, or a failed verification each abort before the
    atomic single-ref push, leaving the remote unchanged.
    """
    if outcome.tree is None:
        return None

    if tuple(outcome.parents) != (head_sha, base_sha):
        raise MergeConflictPublishError(
            f"Validated parents {tuple(outcome.parents)} do not match the "
            f"pinned head/base ({head_sha}, {base_sha})"
        )

    _require_commit(source, head_sha)
    _require_commit(source, base_sha)

    unmerged = _git(source, "ls-files", "--unmerged")
    if unmerged.returncode != 0:
        detail = (unmerged.stderr or unmerged.stdout).strip()
        raise MergeConflictPublishError(f"Could not read the index: {detail}")
    if unmerged.stdout.strip():
        raise MergeConflictPublishError(
            "Unresolved index entries remain in the source checkout"
        )

    commit_sha = _commit_tree(source, outcome.tree, (head_sha, base_sha))

    (verify or _verify_commit)(source, commit_sha, outcome.tree, outcome.parents)

    remote_head = _ls_remote_head(source, remote, head_ref)
    if remote_head != head_sha:
        raise MergeConflictPublishError(
            f"PR head {head_ref!r} moved from {head_sha} to "
            f"{remote_head or '(deleted)'}; refusing to push"
        )

    _push_head(source, remote, head_ref, commit_sha)
    return commit_sha
