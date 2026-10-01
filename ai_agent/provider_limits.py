"""Cached provider limits for the coding agent's dashboard snapshot."""

from __future__ import annotations

import asyncio
import copy
import logging
import time

from ai_agent.codex_limits import read_codex_limits
from ai_agent.config import ANTHROPIC_KEY

logger = logging.getLogger(__name__)
REFRESH_SECONDS = 300
_LIMITS: dict[str, dict] = {}


def limits_snapshot() -> dict:
    """Return cached readings without exposing credentials or mutable state."""
    claude = _LIMITS.get(
        "claude",
        {
            "status": "not_checked" if ANTHROPIC_KEY else "not_configured",
            "message": "Run /limits claude to refresh API rate limits"
            if ANTHROPIC_KEY
            else "Claude API key is not configured",
            "windows": [],
        },
    )
    return copy.deepcopy(
        {
            "codex": _LIMITS.get(
                "codex",
                {
                    "status": "loading",
                    "message": "Checking account limits",
                    "windows": [],
                },
            ),
            "claude": claude,
        }
    )


def cache_claude_limits(status: int, headers: dict[str, str]) -> None:
    """Cache only rate-limit headers from an explicit /limits Claude request."""
    windows = []
    for key, label in [
        ("requests", "Requests"),
        ("input-tokens", "Input tokens"),
        ("output-tokens", "Output tokens"),
        ("tokens", "Tokens"),
    ]:
        prefix = f"anthropic-ratelimit-{key}"
        remaining = headers.get(f"{prefix}-remaining")
        limit = headers.get(f"{prefix}-limit")
        if remaining is None and limit is None:
            continue
        windows.append(
            {
                "bucket": label,
                "remaining": remaining,
                "limit": limit,
                "reset": headers.get(f"{prefix}-reset"),
            }
        )
    _LIMITS["claude"] = {
        "status": "ok" if status < 400 and windows else "unavailable",
        "message": "API rate limits; separate from Claude Code subscription"
        if status < 400
        else f"Claude API returned HTTP {status}",
        "checked_at": time.time(),
        "windows": windows,
    }


async def refresh_codex_limits() -> None:
    """Update the cached quota reading, marking failed refreshes explicitly."""
    try:
        windows = await read_codex_limits()
        _LIMITS["codex"] = {
            "status": "ok" if windows else "unavailable",
            "message": "Account limits" if windows else "No quota windows returned",
            "windows": windows,
            "checked_at": time.time(),
        }
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        asyncio.TimeoutError,
    ) as error:
        logger.warning("Codex limit check failed (%s)", type(error).__name__)
        previous = _LIMITS.get("codex", {})
        _LIMITS["codex"] = {
            **previous,
            "status": "unavailable",
            "message": "Could not refresh limits; check Codex login and CLI version",
        }


async def poll_limits() -> None:
    """Refresh in the background so a slow provider never blocks the heartbeat."""
    while True:
        await refresh_codex_limits()
        await asyncio.sleep(REFRESH_SECONDS)
