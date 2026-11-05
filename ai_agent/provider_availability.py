"""Pure provider-availability classification for the autonomous worker.

The worker picks a tool from the registry's roles and enabled state, then asks
this module whether that tool can run right now. Everything here is a pure
function of its inputs: no probe is launched, no reset time is invented, and a
failure is classified by its text alone. The six status words below are the one
vocabulary the worker and the worker loop share.

Two categories are only meaningful for one of the two questions:

- :data:`EXECUTION` is never an availability answer: a tool that is available
  can still fail for a real, non-limit reason, which is an execution failure.
- :data:`TRANSIENT` is an availability answer (a rate/usage window is exhausted
  or the tool is marked out) and also a failure answer (429/network/rate-limit).

The worker's tool namespace maps ``claude`` to the Claude Code CLI *runner*,
never to the native-Anthropic API tool (``claude-code`` in the provider
registry). The runner may be routed through DeepSeek, where the native-Anthropic
login probe is meaningless and must never gate the runner.
"""

from __future__ import annotations

import re
import time

from ai_agent import provider_registry
from ai_agent.provider_limits import funds_insufficient

DISABLED = "disabled"
AUTH_BLOCKED = "auth_blocked"
FUNDS_INSUFFICIENT = "funds_insufficient"
TRANSIENT = "transient"
EXECUTION = "execution"
AVAILABLE = "available"

STATUSES = (
    DISABLED,
    AUTH_BLOCKED,
    FUNDS_INSUFFICIENT,
    TRANSIENT,
    EXECUTION,
    AVAILABLE,
)

# The worker's tool name -> provider identity. "claude" is the Claude Code CLI
# runner (subscription/token), not the native-Anthropic API tool.
RUNNER_ALIASES = {"claude": "claude-code"}

# Which limits snapshot key each worker tool reads. The runner's subscription
# reading lives under "claude_code"; the API's rate-limit reading is "claude".
LIMIT_KEYS = {
    "codex": "codex",
    "claude": "claude_code",
    "claude-code": "claude_code",
}

# Auth statuses that mean the tool cannot run right now. configured_unverified
# and unknown are deliberately usable: a working API/token route must not be
# disabled, and unknown output never means logged out.
_BLOCKED_AUTH = ("logged_out", "unavailable")

# Failure-text recognition. Funds is checked before transient so "spending
# limit" is never mistaken for a retryable usage limit.
_FUNDS_TEXT = re.compile(
    r"insufficient\s+funds|spending\s+limit|api.?spending|billing|"
    r"credits?|balance|funding",
    re.IGNORECASE,
)
_TRANSIENT_TEXT = re.compile(
    r"usage\s+limit|limit\s+reached|hit\s+your\s+(?:usage\s+)?limit|"
    r"rate.?limit|quota\s+exceeded|too\s+many\s+requests|"
    r"try\s+again|retry|network|connection|time-?out|timed\s+out|"
    r"dns|unreachable|at\s+capacity",
    re.IGNORECASE,
)


def provider_name(tool: str) -> str:
    """The provider identity behind a worker tool name."""
    return RUNNER_ALIASES.get(tool, tool)


def limits_key(tool: str) -> str | None:
    """The limits snapshot key for a worker tool, or None when unreadable."""
    return LIMIT_KEYS.get(tool)


def is_wired(tool: str) -> bool:
    """True when the registry has an enabled provider with roles for this tool."""
    provider = provider_registry.get_provider(provider_name(tool))
    return (
        provider is not None
        and provider_registry.capability(provider, "enabled") is True
        and bool(provider_registry.roles_of(provider))
    )


def _auth_status(auth: dict | None) -> str:
    if not isinstance(auth, dict):
        return "unknown"
    return str(auth.get("status", "unknown"))


def _transient(
    reading: dict | None, now: float, reserve: int, out_until: float | None
) -> bool:
    """A rate window below the reserve or a future out-until marks the tool out."""
    if isinstance(out_until, (int, float)) and not isinstance(out_until, bool):
        if out_until > now:
            return True
    if not isinstance(reading, dict):
        return False
    for window in reading.get("windows") or []:
        if not isinstance(window, dict):
            continue
        remaining = window.get("remaining_percent")
        resets = window.get("resets_at")
        if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
            continue
        if (
            isinstance(resets, (int, float))
            and not isinstance(resets, bool)
            and resets <= now
        ):
            continue  # that window has reset since the reading
        if remaining < reserve:
            return True
    return False


def availability(
    tool: str,
    *,
    auth: dict | None = None,
    limits: dict | None = None,
    now: float | None = None,
    reserve: int = 0,
    out_until: dict | None = None,
) -> str:
    """Classify a worker tool's availability from cached auth and limits.

    ``auth`` is the full auth snapshot (keyed by provider name) and ``limits``
    the full limits snapshot (keyed by limits key); ``out_until`` is the
    worker's saved per-tool map. Returns one of ``disabled``,
    ``auth_blocked``, ``funds_insufficient``, ``transient`` or ``available``.
    """
    now = time.time() if now is None else now
    provider = provider_registry.get_provider(provider_name(tool))
    enabled = (
        provider is not None
        and provider_registry.capability(provider, "enabled") is True
    )
    if not enabled:
        return DISABLED
    record = auth.get(provider_name(tool)) if isinstance(auth, dict) else auth
    if _auth_status(record) in _BLOCKED_AUTH:
        return AUTH_BLOCKED
    key = limits_key(tool)
    reading = limits.get(key) if isinstance(limits, dict) and key else None
    if funds_insufficient(reading):
        return FUNDS_INSUFFICIENT
    until = out_until.get(tool) if isinstance(out_until, dict) else out_until
    if _transient(reading, now, reserve, until):
        return TRANSIENT
    return AVAILABLE


def classify_failure(text: object, *, status_code: int | None = None) -> str:
    """Classify a failure's cause: transient limit, funds, or a real execution.

    Only transient failures are retryable by waiting out a limit; funds and
    execution failures are the run's own fault and never set ``out_until``.
    No reset time is invented here.
    """
    if status_code == 429:
        return TRANSIENT
    text = "" if text is None else str(text)
    if _FUNDS_TEXT.search(text):
        return FUNDS_INSUFFICIENT
    if _TRANSIENT_TEXT.search(text):
        return TRANSIENT
    return EXECUTION
