"""Detect and stop repeated identical permission-denial loops.

A provider loop — the model retrying the same denied tool call — spends money
without progress. Claude Code (stream-json) and Codex (JSON) can report
permission denials as *structured* events, separate from ordinary failed tool
results. This module consumes only those version-verified structured events,
never plain text or quoted log lines, normalizes each denial into a secret-free
identity (runner/tool/operation fingerprint/policy), and raises a blocker once
the same denial repeats past a configurable threshold within one attempt.

When the installed runner does not emit a structured denial event — or its
version cannot be verified — no signal is consumed and the capability gap is
documented in ``docs/denial_loop.md``. Denial is never inferred from text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from ai_agent import config
from ai_agent.run_outcomes import NoCodeOutcomeError, blocked_report

logger = logging.getLogger(__name__)

# Runners whose structured denial events this module understands.
CLAUDE = "claude"
CODEX = "codex"
RUNNERS = (CLAUDE, CODEX)

# Conservative version floors: at/above these the structured denial event is
# documented to exist. A version string that cannot be parsed means
# "unverified" and consumes no signal. See docs/denial_loop.md before raising.
MIN_VERSIONS = {CLAUDE: (2, 0, 0), CODEX: (0, 40, 0)}

# Structured output flags that make a run emit per-event JSON on stdout.
_CLAUDE_STREAM_FLAGS = ("--output-format", "stream-json")
_CODEX_JSON_FLAGS = ("--json",)

_version_cache: dict[str, str | None] = {}


@dataclass(frozen=True)
class DenialEvent:
    """One normalized, secret-free permission-denial observation."""

    runner: str
    tool: str
    operation: str
    policy: str

    def identity(self) -> tuple[str, str, str, str]:
        """The denial identity used to count repeats within one attempt."""
        return (self.runner, self.tool, self.operation, self.policy)


class DenialLoopBlockedError(NoCodeOutcomeError):
    """A repeated identical denial; stop the attempt with no more inference."""

    def __init__(self, event: DenialEvent, count: int) -> None:
        """Build a precise, secret-free owner-visible blocker report."""
        self.event = event
        self.count = count
        summary = (
            f"{event.runner} repeated the same denied operation {count} times "
            "in this attempt."
        )
        evidence = (
            f"tool: {event.tool or 'unknown'}; policy: {event.policy or 'denied'}; "
            f"operation fingerprint: {event.operation or 'unknown'}"
        )
        next_step = (
            "Approve the tool or operation in the plan/allow-list, or change "
            "the denied command, then retry the task to start a fresh attempt."
        )
        super().__init__(blocked_report(summary, evidence, next_step))


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """Return the leading dotted version triple, or None when unverifiable."""
    if not text:
        return None
    match = re.search(r"\d+\.\d+\.\d+", text)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def probe_version(
    runner: str, run: Callable[..., subprocess.CompletedProcess[str]] | None = None
) -> str | None:
    """Return the installed runner's dotted version, or None; cached per runner."""
    if runner not in _version_cache:
        _version_cache[runner] = _probe_version_uncached(runner, run)
    return _version_cache[runner]


def _probe_version_uncached(
    runner: str, run: Callable[..., subprocess.CompletedProcess[str]] | None
) -> str | None:
    executor = run or subprocess.run
    try:
        result = executor(
            [runner, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parsed = parse_version(f"{result.stdout or ''}{result.stderr or ''}")
    return ".".join(str(part) for part in parsed) if parsed else None


def _structured_flag(runner: str, args: list[str]) -> bool:
    """Whether the command already requests a structured per-event envelope."""
    if runner == CLAUDE:
        return _CLAUDE_STREAM_FLAGS[0] in args and _CLAUDE_STREAM_FLAGS[1] in args
    if runner == CODEX:
        return any(flag in args for flag in _CODEX_JSON_FLAGS)
    return False


def supports_structured_denials(
    runner: str, args: list[str], version: str | None
) -> tuple[bool, str]:
    """Whether a run's stdout carries version-verified denial events.

    Returns ``(supported, reason)``; ``reason`` documents the gap when the run
    must not be inspected, so the capability gap stays explicit.
    """
    if runner not in MIN_VERSIONS:
        return False, f"runner {runner!r} is not a known structured-denial emitter"
    parsed = parse_version(version)
    if parsed is None:
        return False, f"runner version unverified ({version!r})"
    if parsed < MIN_VERSIONS[runner]:
        return False, f"runner {version!r} predates structured denial events"
    if not _structured_flag(runner, args):
        return False, "structured output is not enabled for this run"
    return True, "structured denial events available"


def parse_denial_event(line: str, runner: str) -> DenialEvent | None:
    """Return a normalized denial from one structured line, or None.

    Only an explicit structured denial marker is consumed. A ``result`` event
    with ``is_error`` (a failing test) or an ordinary tool error is not a
    denial, and the model's text is never inspected.
    """
    if runner not in RUNNERS:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    if runner == CLAUDE:
        return _claude_denial(event)
    return _codex_denial(event)


def _safe_token(value: object, default: str = "") -> str:
    """Bound a structured label to a safe charset so no secret or prose leaks."""
    text = "" if value is None else str(value)
    text = re.sub(r"[^A-Za-z0-9_.\- ]", "", text).strip()[:80]
    return text or default


def _operation_fingerprint(value: object) -> str:
    """Hash a denied operation so its raw command (and any secret) never leaks."""
    if value is None:
        return ""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _first(value: object, *fallbacks: object) -> object | None:
    if value not in (None, ""):
        return value
    for fallback in fallbacks:
        if fallback not in (None, ""):
            return fallback
    return None


def _claude_denial(event: dict) -> DenialEvent | None:
    """Parse a Claude Code stream-json denial event.

    The documented event is ``{"type": "tool_use_result", "denial": {"denied":
    true, "tool": ..., "operation": ..., "policy": ...}}``. ``denial`` must be a
    dict with an explicit ``denied: true``; anything else is ignored.
    """
    if event.get("type") != "tool_use_result":
        return None
    denial = event.get("denial")
    if not isinstance(denial, dict) or denial.get("denied") is not True:
        return None
    operation = _first(
        denial.get("operation"), denial.get("input"), event.get("tool_input")
    )
    policy = _safe_token(_first(denial.get("policy"), event.get("subtype")), "denied")
    return DenialEvent(
        runner=CLAUDE,
        tool=_safe_token(_first(denial.get("tool"), event.get("tool_name"))),
        operation=_operation_fingerprint(operation),
        policy=policy,
    )


def _codex_denial(event: dict) -> DenialEvent | None:
    """Parse a Codex JSON denial event.

    The documented event is ``{"type": "tool_denied", "denied": true, "tool":
    ..., "operation": ..., "policy": ...}`` (or ``permission_denied``). The
    ``denied`` boolean must be explicitly true.
    """
    if event.get("type") not in ("tool_denied", "permission_denied"):
        return None
    if event.get("denied") is not True:
        return None
    operation = _first(event.get("operation"), event.get("input"))
    return DenialEvent(
        runner=CODEX,
        tool=_safe_token(event.get("tool")),
        operation=_operation_fingerprint(operation),
        policy=_safe_token(_first(event.get("policy"), event.get("type")), "denied"),
    )


class DenialLoopDetector:
    """Count identical structured denials per attempt and raise at the threshold."""

    def __init__(self, max_per_attempt: int | None = None) -> None:
        """Create a counter whose threshold never inspects model text."""
        self.max_per_attempt = (
            config.DENIAL_LOOP_MAX_PER_ATTEMPT
            if max_per_attempt is None
            else max_per_attempt
        )
        self._counts: dict[tuple[str | None, str, str, str, str], int] = {}

    def feed(
        self, line: str, *, runner: str, attempt_id: str | None
    ) -> DenialLoopBlockedError | None:
        """Consume one stdout line; return a blocker at the threshold, else None."""
        if self.max_per_attempt <= 0:
            return None
        event = parse_denial_event(line, runner)
        if event is None:
            return None
        key = (attempt_id, *event.identity())
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count >= self.max_per_attempt:
            return DenialLoopBlockedError(event, count)
        return None

    def reset(self) -> None:
        """Forget all counts (a fresh attempt or a policy change)."""
        self._counts.clear()


def detector_for(runner: str, args: list[str]) -> DenialLoopDetector | None:
    """Return a detector for a run emitting verified structured denials, else None."""
    if config.DENIAL_LOOP_MAX_PER_ATTEMPT <= 0:
        return None
    if runner not in RUNNERS:
        return None
    version = probe_version(runner)
    supported, reason = supports_structured_denials(runner, args, version)
    if not supported:
        logger.info("Denial-loop detection unavailable for %s: %s", runner, reason)
        return None
    return DenialLoopDetector()


__all__ = [
    "CLAUDE",
    "CODEX",
    "DenialEvent",
    "DenialLoopBlockedError",
    "DenialLoopDetector",
    "detector_for",
    "parse_denial_event",
    "parse_version",
    "probe_version",
    "supports_structured_denials",
]
