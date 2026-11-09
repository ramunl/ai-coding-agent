"""Deterministic, bounded summaries of verification output for the model.

Raw verification output (baseline.sh, pytest, ruff, CI job logs) can be huge
and can carry secrets, so it must never enter a prompt or a snapshot as-is.
This module reduces one verification output to a bounded, redacted summary —
identity, exit code, counts, redacted head/tail and an explicit truncation
marker — while the full redacted diagnostics are retained in a private local
artifact for the owner. The summary is deterministic: the same input yields the
same head/tail, counts and artifact digest.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ai_agent.config import USAGE_DETAILS_PATH
from ai_agent.context_validation import _redact_text
from ai_agent.repository_context import repository_excerpt

logger = logging.getLogger(__name__)

HEAD_LINES = 30
TAIL_LINES = 30
MAX_SUMMARY_CHARS = 4000
MAX_ARTIFACT_CHARS = 1_000_000
MAX_EXCERPT_FILES = 8


@dataclass(frozen=True)
class VerificationSummary:
    """A bounded, redacted view of one verification output."""

    identity: str
    exit_code: int
    line_count: int
    total_chars: int
    head: tuple[str, ...]
    tail: tuple[str, ...]
    truncated: bool
    artifact_path: str | None = None
    artifact_sha256: str | None = None

    def text(self) -> str:
        """Render the bounded prompt-facing summary, never raw logs."""
        status = "passed" if self.exit_code == 0 else f"failed ({self.exit_code})"
        lines = [
            f"Verification {self.identity}: {status}",
            f"lines: {self.line_count}, chars: {self.total_chars}",
        ]
        if self.truncated:
            lines.append("output truncated to a bounded head/tail")
        if self.head:
            lines.append("--- head ---")
            lines.extend(self.head)
        if self.tail:
            lines.append("--- tail ---")
            lines.extend(self.tail)
        if self.artifact_path:
            digest = self.artifact_sha256 or "unknown"
            lines.append(
                f"full redacted diagnostics: {self.artifact_path} (sha256 {digest})"
            )
        return "\n".join(lines)


def _redact_output(output: str) -> str:
    """Redact the whole output once; secrets are replaced, structure preserved."""
    return _redact_text(output)


def _apply_char_budget(
    head: tuple[str, ...], tail: tuple[str, ...], max_chars: int
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """Cut the joined head+tail to ``max_chars`` with an explicit marker."""
    joined = "\n".join((*head, *tail))
    if len(joined) <= max_chars:
        return head, tail, False
    marker = "… [summary truncated to char budget]"
    budget = max(0, max_chars - len(marker) - 1)
    kept = joined[:budget].rstrip("\n")
    return (tuple(kept.splitlines()) + (marker,), (), True)


def _artifact_filename(identity: str) -> str:
    """A stable, private filename derived from the summary identity."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16] + ".log"


def _write_artifact(
    redacted: str, identity: str, artifact_dir: Path | None
) -> tuple[str | None, str | None]:
    """Retain full redacted diagnostics privately; never raise, never expose."""
    if artifact_dir is None:
        return None, None
    try:
        directory = Path(artifact_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        bounded = redacted[:MAX_ARTIFACT_CHARS]
        data = bounded.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        target = directory / _artifact_filename(identity)
        target.write_bytes(data)
        return str(target), digest
    except OSError as error:
        logger.warning("Could not retain verification artifact: %s", error)
        return None, None


def summarize_verification(
    output: str | None,
    *,
    identity: str,
    exit_code: int,
    head_lines: int = HEAD_LINES,
    tail_lines: int = TAIL_LINES,
    max_chars: int = MAX_SUMMARY_CHARS,
    artifact_dir: Path | None = None,
) -> VerificationSummary:
    """Reduce one verification output to a bounded, redacted summary.

    ``head_lines``/``tail_lines`` select the redacted head and tail; when the
    output has more lines than the two budgets combined, ``truncated`` is set
    and the middle is elided. ``max_chars`` is the explicit character cap for
    the joined head+tail. Full redacted diagnostics are written to
    ``artifact_dir`` when provided (never raising); otherwise no artifact is
    created and the summary stays purely in-memory.
    """
    head_lines = max(0, int(head_lines))
    tail_lines = max(0, int(tail_lines))
    max_chars = max(1, int(max_chars))
    redacted = _redact_output(output or "")
    lines = redacted.splitlines()
    line_count = len(lines)
    if line_count <= head_lines + tail_lines:
        head = tuple(lines)
        tail: tuple[str, ...] = ()
        truncated = False
    else:
        head = tuple(lines[:head_lines]) if head_lines else ()
        tail = tuple(lines[-tail_lines:]) if tail_lines else ()
        truncated = True
    head, tail, char_truncated = _apply_char_budget(head, tail, max_chars)
    artifact_path, artifact_sha256 = _write_artifact(redacted, identity, artifact_dir)
    return VerificationSummary(
        identity=identity,
        exit_code=int(exit_code),
        line_count=line_count,
        total_chars=len(redacted),
        head=head,
        tail=tail,
        truncated=truncated or char_truncated,
        artifact_path=artifact_path,
        artifact_sha256=artifact_sha256,
    )


def verification_artifact_dir() -> Path:
    """The private local directory for full redacted verification diagnostics."""
    override = os.environ.get("VERIFICATION_ARTIFACT_DIR")
    if override:
        return Path(override).expanduser()
    return USAGE_DETAILS_PATH.parent / "verification"


def file_excerpts(
    refs: Iterable[tuple[str, int | None, int | None]],
    *,
    repo_path: Path | str | None = None,
    max_files: int = MAX_EXCERPT_FILES,
) -> str:
    """Return bounded, redacted excerpts for targeted file ranges.

    Reuses :func:`repository_context.repository_excerpt` (and therefore
    ``_excerpt_block``) so every read keeps the pinned, symlink-safe
    ``repository_read`` path. Verification summaries and repair prompts call
    this to cite failing files without dumping whole files into the model.
    """
    blocks: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if len(blocks) >= max_files:
            blocks.append("… [file excerpt budget reached]")
            break
        if not isinstance(ref, (tuple, list)) or len(ref) != 3:
            continue
        relative, line_start, line_end = ref
        if not isinstance(relative, str) or relative in seen:
            continue
        seen.add(relative)
        block = repository_excerpt(
            relative,
            repo_path=repo_path,
            line_start=line_start,
            line_end=line_end,
        )
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


__all__ = [
    "HEAD_LINES",
    "TAIL_LINES",
    "MAX_SUMMARY_CHARS",
    "VerificationSummary",
    "file_excerpts",
    "summarize_verification",
    "verification_artifact_dir",
]
