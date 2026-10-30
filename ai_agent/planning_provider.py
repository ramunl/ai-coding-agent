"""Send planning prompts to the selected provider and return its text output.

Codex plans through ``codex exec`` and Claude through Claude Code
(claude_code_planner), each on the owner's subscription. CLAUDE_PLANNER_VIA=api
plans with the Anthropic API and ANTHROPIC_API_KEY instead.

The Anthropic client is created on first use. Configuration is read once, at
import, like the rest of the agent: importing ai_agent.config lazily from here
let a test import it without the bot's environment and leak an empty owner
into later tests.
"""

from __future__ import annotations

import functools
import os
import tempfile
from pathlib import Path

import anthropic

from ai_agent.claude_code_planner import claude_code_message
from ai_agent.codex_models import model_args
from ai_agent.config import (
    ANTHROPIC_KEY,
    ANTHROPIC_MODEL,
    CLAUDE_PLANNER_VIA,
    CODEX_TIMEOUT_SECONDS,
    PLANNING_AGENT,
)
from ai_agent.context_packets import context_prompt
from ai_agent.context_sources import ContextRequest
from ai_agent.image_delivery import WARNING, attachments, blocks, run_with_images
from ai_agent.model_errors import codex_capacity_explained, model_error_message
from ai_agent.ops_context import diagnostic_context
from ai_agent.provider_limits import record_claude_response
from ai_agent.shell import run
from ai_agent.token_usage import (
    UsageRecord,
    claude_total_tokens,
    codex_total_tokens,
    record_usage,
)

SCHEMAS_DIR = Path(__file__).parent / "schemas"
SUPPORTED_PLANNING_AGENTS = ("codex", "claude")


def schema_path(schema_name: str) -> Path:
    """Resolve a structured-output schema bundled with the package."""
    return SCHEMAS_DIR / schema_name


@functools.cache
def client() -> anthropic.Anthropic:
    """Create the Anthropic client once, on the first Claude planning request."""
    return anthropic.Anthropic(
        api_key=ANTHROPIC_KEY,
        http_client=anthropic.DefaultHttpxClient(
            event_hooks={"response": [record_claude_response]}
        ),
    )


def normalize_planning_agent(agent: str | None = None) -> str:
    """Resolve the requested planner or reject an unsupported provider."""
    value = (agent or PLANNING_AGENT).strip().lower()
    if value not in SUPPORTED_PLANNING_AGENTS:
        raise ValueError(f"Unsupported planning agent: {value}. Use codex or claude.")
    return value


def planning_agent_label(agent: str | None = None) -> str:
    """Return the display name for the selected planner."""
    return {"codex": "Codex", "claude": "Claude"}[normalize_planning_agent(agent)]


def _codex_message(prompt: str, schema_name: str) -> str:
    with tempfile.NamedTemporaryFile(
        prefix="ai-agent-codex-", suffix=".json"
    ) as output:
        with codex_capacity_explained():
            result = run_with_images(
                [
                    "codex",
                    "exec",
                    *model_args(),
                    "--sandbox",
                    "read-only",
                    "--ephemeral",
                    "--output-schema",
                    str(schema_path(schema_name)),
                    "--output-last-message",
                    output.name,
                    prompt,
                ],
                run,
                timeout=CODEX_TIMEOUT_SECONDS,
            )
        record_usage(UsageRecord("planner", "codex", codex_total_tokens(result.output)))
        return Path(output.name).read_text(encoding="utf-8").strip()


def _create_message(
    *, model: str, max_tokens: int, messages: list[anthropic.types.MessageParam]
) -> anthropic.types.Message:
    """Call the API, translating a retired/invalid model into a clear error."""
    try:
        return client().messages.create(
            model=model, max_tokens=max_tokens, messages=messages
        )
    except anthropic.NotFoundError as error:
        # 404 here means the model string was rejected, not a network fault.
        raise RuntimeError(model_error_message()) from error


def planner_message(
    prompt: str,
    provider: str | None,
    schema_name: str,
    max_tokens: int,
    *,
    context: ContextRequest | None = None,
) -> str:
    """Send a prompt to the selected planner and return its text response."""
    prompt = context_prompt(prompt, context)
    prompt += diagnostic_context()
    selected = normalize_planning_agent(provider)
    if selected == "codex":
        return _codex_message(prompt, schema_name)
    if CLAUDE_PLANNER_VIA != "api":
        return claude_code_message(prompt, schema_path(schema_name))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Claude planning through the API is unavailable because "
            "ANTHROPIC_API_KEY is not configured. Unset CLAUDE_PLANNER_VIA to "
            "plan with Claude Code, or use /planner codex."
        )
    images, missing = attachments()
    if missing:
        prompt += "\n" + WARNING
    try:
        response = _create_message(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": blocks(prompt, images) if images else prompt,
                }
            ],
        )
    except Exception as error:
        if not images:
            raise
        # SDK errors may echo request bodies. Keep only the capacity signal.
        if getattr(error, "status_code", None) == 429:
            raise RuntimeError("Claude image planning usage limit reached.") from None
        raise RuntimeError(
            "Claude image planning failed; retry or switch planner."
        ) from None
    record_usage(
        UsageRecord(
            "planner", "claude", claude_total_tokens(getattr(response, "usage", None))
        )
    )
    text = "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not text:
        raise RuntimeError("Claude returned no text for the plan. Please retry.")
    return text
