"""Read secret-free quota windows from the authenticated Codex app-server."""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal

LIMIT_TIMEOUT_SECONDS = 20


def quota_windows(result: dict) -> list[dict]:
    """Prefer named buckets, retaining only displayable quota window fields."""
    buckets = result.get("rateLimitsByLimitId") or {"codex": result.get("rateLimits")}
    if not isinstance(buckets, dict):
        raise ValueError("Invalid quota bucket response")
    windows = []
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            if (
                isinstance(used, bool)
                or not isinstance(used, (int, float))
                or not math.isfinite(used)
            ):
                continue
            windows.append(
                {
                    "bucket": str(bucket.get("limitName") or bucket_id)[:100],
                    "remaining_percent": max(0, min(100, 100 - used)),
                    "window_minutes": window.get("windowDurationMins"),
                    "resets_at": window.get("resetsAt"),
                }
            )
    return windows


async def _send(process: asyncio.subprocess.Process, message: dict) -> None:
    if process.stdin is None:
        raise RuntimeError("Codex input unavailable")
    process.stdin.write((json.dumps(message) + "\n").encode())
    await process.stdin.drain()


async def _response(process: asyncio.subprocess.Process, request_id: int) -> dict:
    if process.stdout is None:
        raise RuntimeError("Codex output unavailable")
    while line := await process.stdout.readline():
        message = json.loads(line)
        if message.get("id") != request_id:
            continue
        if "error" in message:
            # Do not publish raw provider errors, which may contain identifiers.
            raise RuntimeError(
                "Codex metadata unavailable; check login and CLI version"
            )
        return message["result"]
    raise RuntimeError("Codex app-server exited before returning metadata")


async def _read(process: asyncio.subprocess.Process, method: str, params: dict) -> dict:
    await _send(
        process,
        {
            "method": "initialize",
            "id": 1,
            "params": {"clientInfo": {"name": "ai_dashboard", "version": "1.0"}},
        },
    )
    await _response(process, 1)
    await _send(process, {"method": "initialized", "params": {}})
    await _send(process, {"method": method, "params": params, "id": 2})
    return await _response(process, 2)


async def read_codex_response(method: str, params: dict) -> dict:
    """Read CLI metadata without inference; terminate the process on all exits."""
    process = await asyncio.create_subprocess_exec(
        "codex",
        "app-server",
        "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        result = await asyncio.wait_for(
            _read(process, method, params), LIMIT_TIMEOUT_SECONDS
        )
        return result
    finally:
        # Helpers inherit pipes: kill the group even if the parent already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def read_codex_limits() -> list[dict]:
    """Read account limits without inference."""
    return quota_windows(await read_codex_response("account/rateLimits/read", {}))
