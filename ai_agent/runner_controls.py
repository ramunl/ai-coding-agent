"""Version-gated context/output-control flags for CLI implementers.

The bot can only bound repository reads and verification output that it itself
passes into a prompt. Tool results the model reads *inside* the CLI cannot be
post-processed by the bot, so bounding there is limited to runner-supported
flags/settings plus a prompt contract. This module mirrors
``ai_agent.denial_loop``: a runner's candidate flags are added only when the
installed release is verified to support them, and the gap is logged otherwise.

Today no claude-code or codex release has been verified to expose official
context/output-bounding controls (see ``docs/eff04_runner_controls.md``), so the
candidate flag lists are empty and :func:`supports_context_control` returns
False for every version. Nothing is invented or emulated.
"""

from __future__ import annotations

import logging

from ai_agent.denial_loop import parse_version, probe_version

logger = logging.getLogger(__name__)

CLAUDE = "claude"
CODEX = "codex"
RUNNERS = (CLAUDE, CODEX)

# Version floors at/above which the official context/output-control flags are
# documented to exist. None = no release has been verified yet, so nothing is
# enabled. Raise a floor only after verifying the flags on the VPS and updating
# docs/eff04_runner_controls.md.
MIN_VERSIONS: dict[str, tuple[int, ...] | None] = {CLAUDE: None, CODEX: None}

# Candidate flags that would bound repository reads/output once a release is
# verified to support them. Kept empty until then (docs/eff04_runner_controls.md);
# bounding via invented or unverified flags is forbidden.
_CONTEXT_CONTROL_FLAGS: dict[str, tuple[str, ...]] = {CLAUDE: (), CODEX: ()}


def _flag_present(runner: str, args: list[str]) -> bool:
    """Whether any candidate control flag is already present in ``args``."""
    for flag in _CONTEXT_CONTROL_FLAGS.get(runner, ()):
        if flag in args:
            return True
    return False


def supports_context_control(
    runner: str, args: list[str], version: str | None
) -> tuple[bool, str]:
    """Whether a run may use version-verified context/output-control flags.

    Returns ``(supported, reason)``; ``reason`` documents the gap when the flags
    must not be added, so a missing capability stays explicit instead of being
    emulated with invented flags.
    """
    if runner not in MIN_VERSIONS:
        return False, f"runner {runner!r} is not a known context-control runner"
    parsed = parse_version(version)
    if parsed is None:
        return False, f"runner version unverified ({version!r})"
    floor = MIN_VERSIONS[runner]
    if floor is None:
        return False, (
            f"no {runner} release is verified to expose context/output controls"
        )
    if parsed < floor:
        return False, f"runner {version!r} predates context/output controls"
    if _flag_present(runner, args):
        return False, "context/output controls already enabled for this run"
    return True, "context/output-control flags available"


def context_control_args(runner: str, args: list[str] | None = None) -> list[str]:
    """Version-gated context/output-control flags for one command, or [].

    Logs and omits unsupported flags; never invents them. With no candidate
    flags defined for the runner (the current state) this is a no-op that does
    not probe the runner.
    """
    candidate = _CONTEXT_CONTROL_FLAGS.get(runner)
    if not candidate:
        logger.info(
            "No context/output-control flags defined for %s; none added", runner
        )
        return []
    args = list(args or [])
    version = probe_version(runner)
    supported, reason = supports_context_control(runner, args, version)
    if not supported:
        logger.info("Context/output controls unavailable for %s: %s", runner, reason)
        return []
    return list(candidate)


__all__ = [
    "CLAUDE",
    "CODEX",
    "RUNNERS",
    "context_control_args",
    "supports_context_control",
]
