"""Cached provider limits for the coding agent's dashboard snapshot."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ai_agent.codex_limits import read_codex_limits
from ai_agent.config import ANTHROPIC_KEY, CLAUDE_CODE_LIMITS_FILE

logger = logging.getLogger(__name__)
REFRESH_SECONDS = 300
_LIMITS: dict[str, dict] = {}
CLAUDE_CACHE_FILE = Path(
    os.environ.get(
        "CLAUDE_API_LIMITS_FILE", "/var/lib/ai-coding-agent/claude-api-limits.json"
    )
)


def saved_claude_limits() -> dict:
    """Read only persisted public limit fields; never make a provider request."""
    try:
        value = json.loads(CLAUDE_CACHE_FILE.read_text())
        if isinstance(value, dict) and isinstance(value.get("windows"), list):
            return value
    except (OSError, ValueError):
        pass
    return {}


class LimitResponse(Protocol):
    """Response fields shared by supported Anthropic SDK transports."""

    status_code: int
    headers: Mapping[str, str]


def record_claude_response(response: LimitResponse) -> None:
    """Capture rate-limit headers from actual planning API responses."""
    cache_claude_limits(response.status_code, dict(response.headers))


def limits_snapshot() -> dict:
    """Return cached readings without exposing credentials or mutable state."""
    claude = _LIMITS.get(
        "claude",
        saved_claude_limits()
        or {
            "status": "not_checked" if ANTHROPIC_KEY else "not_configured",
            "message": "No reading yet; recorded automatically from Claude API usage"
            if ANTHROPIC_KEY
            else "Claude API key is not configured",
            "windows": [],
        },
    )
    from ai_agent.claude_code_limits import read_limits

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
            "claude_code": read_limits(CLAUDE_CODE_LIMITS_FILE),
        }
    )


def cache_claude_limits(status: int, headers: dict[str, str]) -> None:
    """Cache public rate-limit headers from real API usage without probing."""
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
    if not windows:
        return
    _LIMITS["claude"] = {
        "status": "ok" if status < 400 and windows else "unavailable",
        "message": "API rate limits; separate from Claude Code subscription"
        if status < 400
        else f"Claude API returned HTTP {status}",
        "checked_at": time.time(),
        "windows": windows,
    }

    from ai_agent.bot.atomic_file import write_json_atomic

    write_json_atomic(CLAUDE_CACHE_FILE, _LIMITS["claude"])


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
