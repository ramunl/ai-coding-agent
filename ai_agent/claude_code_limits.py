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


def capture_limits(payload: dict, path: Path, now: float | None = None) -> bool:
    """Persist quota fields only; sessions without quotas leave existing data intact."""
    data = payload.get("rate_limits")
    if not isinstance(data, dict):
        return False
    current = time.time() if now is None else now
    return write_json_atomic(
        path,
        {
            "checked_at": current,
            "windows": quota_windows(data, current),
        },
    )


def read_limits(path: Path, now: float | None = None) -> dict:
    """Read saved subscription windows, dropping expired or invalid readings."""
    waiting = {
        "status": "not_checked",
        "windows": [],
        "message": "Waiting for an interactive Claude Code session on this server",
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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
    except (OSError, ValueError, TypeError, KeyError) as error:
        logger.warning("Claude Code limits unreadable (%s)", type(error).__name__)
        return {
            **waiting,
            "status": "unavailable",
            "message": "Claude Code reading unavailable",
        }
    return {
        "status": "ok" if windows else "unavailable",
        "windows": windows,
        "checked_at": checked,
        "message": "Subscription limits from status-line data"
        if windows
        else "Previous quota windows expired; waiting for Claude Code",
    }
