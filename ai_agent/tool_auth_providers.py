"""Read-only CLI status adapters; never return provider text or credentials."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from dataclasses import dataclass

from ai_agent.config import ANTHROPIC_KEY

PROBE_TIMEOUT = 5.0
OUTPUT_LIMIT = 16_384


@dataclass(frozen=True)
class Reading:
    """An allow-listed observation, not proof of future API access."""

    status: str
    method: str = "unknown"
    # Only a provider-documented mandatory deadline may populate this field.
    relogin_at: float | None = None


async def run_probe(*args: str) -> tuple[int, str]:
    """Bound output and duration; kill and reap the process group on every exit."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        limit=OUTPUT_LIMIT + 1,
    )
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > OUTPUT_LIMIT:
                    raise ValueError("probe_output_limit")
            code = await process.wait()
            return code, output.decode("utf-8", errors="replace")
    finally:
        # Descendants can retain pipe handles even after the CLI itself exits.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        # Drain after killing: a full pipe can otherwise prevent wait() finishing.
        while await process.stdout.read(4096):
            pass
        await process.wait()


def parse_codex(code: int, output: str) -> Reading:
    """Recognize explicit status lines only; API-key storage is unverified."""
    lines = output.strip().splitlines()
    if code == 0 and "Logged in using ChatGPT" in lines:
        return Reading("authenticated", "oauth")
    if code == 0 and any(
        re.fullmatch(r"Logged in using an API key(?: - .*)?", line) for line in lines
    ):
        return Reading("configured_unverified", "api_key")
    if code == 1 and output.strip() == "Not logged in":
        return Reading("logged_out", "none")
    return Reading("unknown")


def parse_claude(code: int, output: str) -> Reading:
    """Ignore identities, expiry fields, raw errors and unknown auth backends."""
    try:
        data = json.loads(output)
    except (ValueError, RecursionError):
        return Reading("unknown")
    if not isinstance(data, dict):
        return Reading("unknown")
    method = data.get("authMethod")
    logged_in = data.get("loggedIn")
    if code == 1 and logged_in is False and method == "none":
        return Reading("logged_out", "none")
    if code != 0 or logged_in is not True:
        return Reading("unknown")
    if method in ("claude.ai", "oauth_token"):
        return Reading("authenticated", "oauth")
    if method in ("api_key", "api_key_helper", "third_party"):
        return Reading("configured_unverified", method)
    return Reading("unknown")


async def check(tool: str) -> Reading:
    """Use runtime credentials and advertised status commands, never inference."""
    if tool == "claude":
        return Reading(
            "configured_unverified" if ANTHROPIC_KEY else "logged_out",
            "api_key" if ANTHROPIC_KEY else "none",
        )
    binary, group = ("codex", "login") if tool == "codex" else ("claude", "auth")
    code, help_text = await run_probe(binary, group, "--help")
    if code != 0 or not re.search(r"(?m)^\s+status(?:\s|\[)", help_text):
        return Reading("unavailable")
    if tool == "claude-code":
        code, help_text = await run_probe(binary, group, "status", "--help")
        if code != 0 or "--json" not in help_text:
            return Reading("unavailable")
        code, output = await run_probe(binary, group, "status", "--json")
        return parse_claude(code, output)
    code, output = await run_probe(binary, group, "status")
    return parse_codex(code, output)
