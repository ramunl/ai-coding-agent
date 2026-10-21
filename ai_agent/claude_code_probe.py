"""Keep the Claude Code limits fresh without an interactive session.

Claude Code reports its subscription limits to the status line, which only an
interactive session runs, so the dashboard's reading went stale while the bot
used ``claude -p``. In stream-json output Claude Code also emits a
``rate_limit_event`` with the same windows. Every PROBE_MINUTES this sends one
tiny prompt to the cheapest model and records those windows in the same file
the status line writes (claude_code_limits), so the dashboard and the worker's
limit checks see current numbers. Costs one small request per probe; set
CLAUDE_LIMITS_PROBE_MINUTES=0 to switch it off.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ai_agent.claude_code_limits import capture_limits

logger = logging.getLogger(__name__)

PROBE_MINUTES = int(os.environ.get("CLAUDE_LIMITS_PROBE_MINUTES", "30"))
PROBE_MODEL = os.environ.get("CLAUDE_LIMITS_PROBE_MODEL", "haiku")
TIMEOUT_SECONDS = 120
START_DELAY_SECONDS = 90  # let the bot finish starting first
WINDOW_NAMES = ("five_hour", "seven_day")

# argv -> stdout ("" when it can't run).
Runner = Callable[[list[str]], str]


def run(argv: list[str]) -> str:
    """Run the probe; its stdout, or "" if Claude Code is missing or fails."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.info("Claude limits probe failed: %s", error)
        return ""
    return result.stdout


def command(model: str = PROBE_MODEL) -> list[str]:
    """One tiny print-mode turn whose events include the rate limits."""
    return [
        "claude",
        "-p",
        "Reply with OK.",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "1",
    ]


def _percent(fraction: object) -> float | None:
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
        return None
    return max(0.0, min(100.0, float(fraction) * 100))


def rate_limits(output: str) -> dict:
    """The status-line shape {"five_hour": {used_percentage, resets_at}, ...}."""
    found: dict[str, dict] = {}
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info")
        if not isinstance(info, dict):
            continue
        windows = info.get("unifiedWindows")
        if isinstance(windows, dict):
            for name in WINDOW_NAMES:
                window = windows.get(name)
                if isinstance(window, dict):
                    used = _percent(window.get("utilization"))
                    if used is not None and isinstance(window.get("resetsAt"), int):
                        found[name] = {
                            "used_percentage": used,
                            "resets_at": window["resetsAt"],
                        }
        kind = info.get("rateLimitType")
        used = _percent(info.get("utilization"))
        if kind in WINDOW_NAMES and used is not None and kind not in found:
            if isinstance(info.get("resetsAt"), int):
                found[kind] = {"used_percentage": used, "resets_at": info["resetsAt"]}
    return found


def probe(path: Path, runner: Runner = run, now: float | None = None) -> bool:
    """Run one probe; True when fresh windows were saved."""
    if not shutil.which("claude") and runner is run:
        return False
    limits = rate_limits(runner(command()))
    if not limits:
        return False  # keep the last reading rather than blank it
    return capture_limits({"rate_limits": limits}, path, now)


async def probe_forever(path: Path, minutes: int = PROBE_MINUTES) -> None:
    """Probe every `minutes`; 0 switches it off."""
    if minutes <= 0:
        return
    await asyncio.sleep(START_DELAY_SECONDS)
    while True:
        try:
            await asyncio.to_thread(probe, path)
        except Exception as error:  # the next probe must still run
            logger.warning("Claude limits probe failed: %s", error)
        await asyncio.sleep(minutes * 60)
