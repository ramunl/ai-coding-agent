"""Capture documented Claude Code status-line quota data for the dashboard."""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

from ai_agent.bot.atomic_file import write_json_atomic

logger = logging.getLogger(__name__)
WINDOWS = {"five_hour": 300, "seven_day": 10080}
FRESH_SECONDS = 3600
ERROR_MESSAGES = {
    "api_spending_limit": "CLI API spending blocked; subscription quota unknown",
    "authentication_conflict": "CLI auth override conflicts with subscription login",
    "authentication_failed": "Claude CLI authentication failed",
    "cli_failed": "Claude CLI quota collection failed",
    "timeout": "Claude CLI quota collection timed out",
    "cli_unavailable": "Claude CLI unavailable",
    "no_valid_windows": "Collection returned no valid subscription quota windows",
}


def quota_windows(data: dict, now: float) -> list[dict]:
    """Retain only supported, unexpired subscription windows and numeric usage."""
    windows = []
    for name, minutes in WINDOWS.items():
        value = data.get(name)
        if not isinstance(value, dict):
            continue
        used, reset = value.get("used_percentage"), value.get("resets_at")
        if not all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(item)
            for item in (used, reset)
        ):
            continue
        if reset <= now:
            continue
        windows.append(
            {
                "bucket": "Claude Code",
                "window_minutes": minutes,
                "remaining_percent": max(0, min(100, 100 - used)),
                "resets_at": reset,
            }
        )
    return windows


def record_attempt(
    path: Path,
    now: float,
    source: str,
    error: str | None,
    *,
    auth_conflict: bool = False,
    windows: list[dict] | None = None,
) -> bool:
    """Keep the successful reading independent of the latest collection attempt."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        data = (
            {
                key: previous[key]
                for key in ("checked_at", "windows", "source")
                if key in previous
            }
            if isinstance(previous, dict)
            else {}
        )
    except (OSError, ValueError):
        data = {}
    data.update(
        attempted_at=now,
        attempt_source=source,
        collection_error=error,
        auth_conflict=auth_conflict,
    )
    if windows:
        data.update(checked_at=now, windows=windows, source=source)
    return write_json_atomic(path, data)


def capture_limits(
    payload: dict,
    path: Path,
    now: float | None = None,
    *,
    source: str = "status_line",
) -> bool:
    """Persist valid quotas; empty or malformed collection preserves the reading."""
    data = payload.get("rate_limits")
    if "rate_limits" not in payload:
        return False  # Status lines without this field are not quota attempts.
    current = time.time() if now is None else now
    windows = quota_windows(data, current) if isinstance(data, dict) else []
    saved = record_attempt(
        path, current, source, None if windows else "no_valid_windows", windows=windows
    )
    return bool(windows) and saved


def read_limits(path: Path, now: float | None = None) -> dict:
    """Read saved subscription windows, dropping expired or invalid readings."""
    waiting = {
        "status": "not_checked",
        "windows": [],
        "freshness": "unknown",
        "collection_error": None,
        "attempted_at": None,
        "attempt_source": None,
        "source": None,
        "auth_conflict": False,
        "message": "No reading yet; recorded from Claude Code status-line usage",
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        metadata = {
            key: data.get(key, waiting[key])
            for key in (
                "attempted_at",
                "attempt_source",
                "source",
                "collection_error",
                "auth_conflict",
            )
        }
        if "checked_at" not in data:
            return {
                **waiting,
                **metadata,
                "status": "unavailable",
                "message": ERROR_MESSAGES.get(
                    metadata["collection_error"], "Subscription quota collection failed"
                ),
            }
        checked = float(data["checked_at"])
        if not math.isfinite(checked):
            raise ValueError("Invalid reading time")
        # Revalidate file contents, not just the status-line input.
        mapped = {
            name: {
                "used_percentage": 100 - value["remaining_percent"],
                "resets_at": value["resets_at"],
            }
            for value in data["windows"]
            for name, minutes in WINDOWS.items()
            if value["window_minutes"] == minutes
        }
        windows = quota_windows(mapped, time.time() if now is None else now)
    except FileNotFoundError:
        return waiting
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        logger.warning("Claude Code limits unreadable (%s)", type(error).__name__)
        return {
            **waiting,
            "status": "unavailable",
            "message": "Claude Code reading unavailable",
        }
    current = time.time() if now is None else now
    fresh = bool(windows) and 0 <= current - checked <= FRESH_SECONDS
    return {
        **metadata,
        "freshness": "fresh" if fresh else "stale",
        "status": "ok" if fresh and not metadata["collection_error"] else "unavailable",
        "windows": windows,
        "checked_at": checked,
        "message": ERROR_MESSAGES.get(
            metadata["collection_error"], "Subscription quota collection failed"
        )
        if metadata["collection_error"]
        else "Subscription limits; last known reading"
        if windows
        else "Last subscription reading; quota windows have expired",
    }
