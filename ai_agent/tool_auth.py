"""Cached, secret-free authentication observations for the Coding snapshot."""

from __future__ import annotations

import asyncio
import logging
import math
import time

# ``check`` is re-exported as the single auth probe: ai_tools' built-in tools
# call it lazily, and tests patch this module's name to intercept every probe.
from ai_agent.tool_auth_providers import Reading, check  # noqa: F401

logger = logging.getLogger(__name__)
POLL_SECONDS = 300
STALE_SECONDS = 900
WARNING_SECONDS = 7 * 86400


def _tools() -> list:
    """Return the registry tools that declare an auth observation."""
    from ai_agent.ai_tools import auth_tools

    return auth_tools()


def auth_record(reading: Reading, checked_at: float | None, stale: bool) -> dict:
    """Project trusted adapter values into the public contract."""
    deadline = reading.relogin_at
    if deadline is not None and (not math.isfinite(deadline) or deadline <= 0):
        deadline = None
    remaining = None if deadline is None else max(0, deadline - time.time())
    warnings = []
    if reading.status == "logged_out" or remaining == 0:
        warnings.append("login_required")
    elif remaining is not None and remaining <= WARNING_SECONDS:
        warnings.append("relogin_soon")
    if reading.status in ("unknown", "unavailable"):
        warnings.append("check_unavailable")
    if stale:
        warnings.append("auth_stale")
    return {
        "status": reading.status,
        "method": reading.method,
        "checked_at": checked_at,
        "stale": stale,
        "relogin_at": deadline,
        "days_remaining": None if remaining is None else math.ceil(remaining / 86400),
        "countdown_support": (
            "supported" if deadline is not None else "no_authoritative_deadline"
        ),
        "warnings": warnings,
    }


class ToolAuth:
    """Own observations on the event loop; snapshot reads never launch probes."""

    def __init__(self) -> None:
        """Start unknown and stale until the first successful observation."""
        self._readings: dict[str, tuple[Reading, float | None, bool]] = {}

    def snapshot(self) -> dict[str, dict]:
        """Return new records, computing age and countdown at publication time."""
        now = time.time()
        result = {}
        for tool in _tools():
            reading, checked_at, failed = self._readings.get(
                tool.name, (Reading("unknown"), None, True)
            )
            stale = failed or checked_at is None or now - checked_at >= STALE_SECONDS
            result[tool.name] = auth_record(reading, checked_at, stale)
            if failed and "check_unavailable" not in result[tool.name]["warnings"]:
                result[tool.name]["warnings"].append("check_unavailable")
        return result

    async def refresh(self) -> None:
        """Retain prior evidence on failure, with its original time and stale flag."""
        for tool in _tools():
            try:
                reading = await tool.auth()
            except (OSError, ValueError, TimeoutError):
                # Never log exceptions: OS errors can contain credential paths.
                logger.warning("Authentication check failed for %s", tool.name)
                reading = Reading("unavailable")
            failed = reading.status in ("unknown", "unavailable")
            if failed and tool.name in self._readings:
                previous, checked_at, _ = self._readings[tool.name]
                self._readings[tool.name] = (previous, checked_at, True)
            else:
                self._readings[tool.name] = (reading, time.time(), failed)

    async def refresh_forever(self, interval: float = POLL_SECONDS) -> None:
        """Poll independently of snapshot ticks until cancelled."""
        while True:
            await self.refresh()
            await asyncio.sleep(interval)
