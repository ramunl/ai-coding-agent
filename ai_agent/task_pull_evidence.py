"""Explicit PR evidence states and repaired-head provenance.

A pull request lookup ends in exactly one of four states, so callers never
have to guess what "no result" meant:

- ``absent``: GitHub answered and there is no pull request to find.
- ``verified``: a concrete archived PR was re-checked and still matches.
- ``mismatched``: a PR exists but does not match the archived attempt.
- ``unavailable``: the lookup failed or the answer was malformed.

``absent`` may settle a task; ``mismatched`` and ``unavailable`` leave it for
the next pass or owner review. Branch searches only discover a candidate; they
never bind a completion by themselves.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum

_SHA = re.compile(r"[0-9a-f]{40}")


class EvidenceStatus(StrEnum):
    """The four outcomes of a bounded PR lookup."""

    ABSENT = "absent"
    VERIFIED = "verified"
    MISMATCHED = "mismatched"
    UNAVAILABLE = "unavailable"


class HeadProvenance(StrEnum):
    """How a live PR head relates to the archived attempt commits."""

    EXACT = "exact"
    REPAIRED = "repaired"
    UNRELATED = "unrelated"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class PullEvidence:
    """One classified lookup with an actionable, owner-safe reason.

    ``pull`` holds verified facts (or a discovered candidate that failed to
    bind). It is never applied unless the status is ``VERIFIED``.
    """

    status: EvidenceStatus
    pull: dict | None = None
    reason: str = ""


def _git_available(repo_path: str, *args: str) -> bool:
    """Run one read-only Git query; False when Git or the object is missing."""
    result = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def classify_head(repo_path: str, commits: set[str], head: str) -> HeadProvenance:
    """Classify a live PR head against the archived attempt commits.

    ``EXACT``: the head is one of the archived commits (no repair). ``REPAIRED``:
    the head differs but every archived commit is an ancestor, so CI or manual
    repairs were built on the archived work. ``UNRELATED``: an archived commit
    is not an ancestor, so the PR cannot belong to this attempt.
    ``UNVERIFIABLE``: the checkout cannot prove ancestry (missing or shallow
    objects); the caller must block for explicit owner review.
    """
    if not isinstance(commits, set) or not commits:
        raise ValueError("Missing archived commit evidence")
    if not all(
        isinstance(sha, str) and _SHA.fullmatch(sha) for sha in commits
    ):
        raise ValueError("Invalid archived commit")
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        raise ValueError("Invalid PR head")
    if head in commits:
        return HeadProvenance.EXACT
    try:
        for commit in commits:
            if not _git_available(repo_path, "cat-file", "-e", commit + "^{commit}"):
                return HeadProvenance.UNVERIFIABLE
            if not _git_available(
                repo_path, "cat-file", "-e", head + "^{commit}"
            ):
                return HeadProvenance.UNVERIFIABLE
            if not _git_available(
                repo_path, "merge-base", "--is-ancestor", commit, head
            ):
                return HeadProvenance.UNRELATED
    except (OSError, subprocess.SubprocessError):
        return HeadProvenance.UNVERIFIABLE
    return HeadProvenance.REPAIRED
