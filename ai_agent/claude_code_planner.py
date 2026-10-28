"""Plan with Claude Code on the owner's Claude subscription, like Codex does.

The Codex planner runs ``codex exec`` logged in with a ChatGPT plan; this runs
``claude -p`` logged in with a Claude plan, so planning with Claude needs no
ANTHROPIC_API_KEY. Claude Code may only read and search the repository (Read,
Grep, Glob): editing, shell commands and the web are refused, as in Codex's
read-only sandbox. The prompt (with any task images) goes in on stdin as one
stream-json user message, and --json-schema makes the reply the schema's JSON
(``structured_output``), as --output-schema does for Codex.

The stream also carries the token usage, which is counted, and the
subscription's rate-limit windows, which refresh the Claude limits the
dashboard and the worker read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from ai_agent.claude_code_limits import capture_limits
from ai_agent.claude_code_probe import rate_limits
from ai_agent.config import (
    CLAUDE_CODE_LIMITS_FILE,
    CLAUDE_PLANNER_MODEL,
    CODEX_TIMEOUT_SECONDS,
)
from ai_agent.image_delivery import WARNING, attachments, blocks
from ai_agent.shell import run
from ai_agent.token_usage import UsageRecord, record_usage

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = ("Read", "Grep", "Glob")
DENIED_TOOLS = ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch")
DETAIL_LENGTH = 1000


def schema_argument(schema: Path) -> str:
    """The schema as one compact argument, without its "$schema" draft URL.

    Claude Code's validator does not load the 2020-12 meta-schema and rejects
    a schema that names it; the schema itself is the same.
    """
    data = json.loads(schema.read_text(encoding="utf-8"))
    data.pop("$schema", None)
    return json.dumps(data, separators=(",", ":"))


def command(schema: Path, model: str = CLAUDE_PLANNER_MODEL) -> list[str]:
    """A read-only, print-mode Claude Code turn reading its prompt from stdin."""
    return [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--allowedTools",
        ",".join(ALLOWED_TOOLS),
        "--disallowedTools",
        ",".join(DENIED_TOOLS),
        "--json-schema",
        schema_argument(schema),
        *(["--model", model] if model else []),
    ]


def message(prompt: str, images: list[tuple[str, bytes]]) -> str:
    """The stream-json user message for stdin."""
    content = blocks(prompt, images) if images else prompt
    envelope = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": "",
    }
    return json.dumps(envelope) + "\n"


def _events(text: str) -> list[dict]:
    found = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            found.append(event)
    return found


def result_event(text: str) -> dict | None:
    """The final ``result`` event of a stream, or None."""
    results = [event for event in _events(text) if event.get("type") == "result"]
    return results[-1] if results else None


def _plain_lines(text: str) -> str:
    """Lines that are not stream events (Claude Code's own stderr)."""
    lines = []
    for line in text.splitlines():
        try:
            json.loads(line)
        except ValueError:
            if line.strip() and not line.startswith("Command failed"):
                lines.append(line.strip())
    return "\n".join(lines[-5:])


def json_text(text: str) -> str:
    """The JSON object in a reply, without a code fence or prose around it."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            json.loads(candidate)
        except ValueError:
            return text
        return candidate
    return text


def _tokens(event: dict) -> int | None:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = [usage.get("input_tokens"), usage.get("output_tokens")]
    if all(isinstance(n, int) and not isinstance(n, bool) for n in counts):
        return counts[0] + counts[1]
    return None


def _save_limits(output: str, limits_file: Path) -> None:
    limits = rate_limits(output)
    if not limits:
        return
    try:
        capture_limits({"rate_limits": limits}, limits_file)
    except Exception as error:  # limits are a bonus; the plan still counts
        logger.info("Could not save Claude limits from planning: %s", error)


def _failure(detail: str) -> RuntimeError:
    detail = " ".join(detail.split())[:DETAIL_LENGTH] or "no details"
    return RuntimeError(f"Claude Code planning failed: {detail}")


def claude_code_message(
    prompt: str,
    schema: Path,
    runner: Callable = run,
    limits_file: Path = CLAUDE_CODE_LIMITS_FILE,
) -> str:
    """Plan with Claude Code; return its JSON reply as text."""
    images, missing = attachments()
    if missing:
        prompt += "\n" + WARNING
    try:
        result = runner(
            command(schema),
            input_text=message(prompt, images),
            private=bool(images),
            timeout=CODEX_TIMEOUT_SECONDS,
        )
    except RuntimeError as error:
        text = str(error)
        _save_limits(text, limits_file)
        event = result_event(text)
        if event is not None:
            raise _failure(str(event.get("result") or "")) from None
        if images:  # a private failure is already safe to show
            raise
        raise _failure(_plain_lines(text)) from None
    output = result.output
    _save_limits(output, limits_file)
    event = result_event(output)
    if event is None:
        raise _failure(_plain_lines(output) or "Claude Code returned no result")
    if event.get("is_error"):
        raise _failure(str(event.get("result") or event.get("subtype") or ""))
    record_usage(UsageRecord("planner", "claude", _tokens(event)))
    structured = event.get("structured_output")
    if isinstance(structured, dict):
        return json.dumps(structured)
    text = json_text(str(event.get("result") or ""))
    if not text:
        raise RuntimeError("Claude returned no text for the plan. Please retry.")
    return text
