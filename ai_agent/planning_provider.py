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

from ai_agent.ai_tools import get_tool, tools_for_role
from ai_agent.claude_code_planner import claude_code_message
from ai_agent.codex_models import DEFAULT_MODEL, current_model, model_args
from ai_agent.config import (
    ANTHROPIC_KEY,
    ANTHROPIC_MODEL,
    CLAUDE_PLANNER_VIA,
    CODEX_TIMEOUT_SECONDS,
    PLANNING_AGENT,
)
from ai_agent.context_lifecycle import current_context
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
    claude_usage_fields,
    codex_total_tokens,
    record_usage,
)
from ai_agent.usage_details import (
    UsageDetail,
    bounded_output_chars,
    current_attempt_id,
    new_call_id,
    record_detail,
)

SCHEMAS_DIR = Path(__file__).parent / "schemas"
SUPPORTED_PLANNING_AGENTS = tuple(tools_for_role("planner"))


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
    supported = tools_for_role("planner")
    if value not in supported:
        raise ValueError(
            f"Unsupported planning agent: {value}. Use {' or '.join(supported)}."
        )
    return value


def planning_agent_label(agent: str | None = None) -> str:
    """Return the display name for the selected planner."""
    selected = normalize_planning_agent(agent)
    tool = get_tool(selected)
    return tool.label if tool else selected


def _codex_message(prompt: str, schema_name: str) -> str:
    call_id = new_call_id()
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
        tokens = codex_total_tokens(result.output)
        text = Path(output.name).read_text(encoding="utf-8").strip()
        record_usage(UsageRecord("planner", "codex", tokens))
        record_detail(
            UsageDetail(
                role="planner",
                runner="codex",
                provider="codex",
                model=_codex_model(),
                attempt_id=current_attempt_id(),
                call_id=call_id,
                total_tokens=tokens,
                output_chars=bounded_output_chars(text),
            )
        )
        return text


def _codex_model() -> str | None:
    """The Codex model actually selected, or None when the CLI default decides."""
    model = current_model()
    return None if model == DEFAULT_MODEL else model


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


def _claude_planner_message(prompt: str, schema_name: str, max_tokens: int) -> str:
    """Plan with Claude Code or the API, as CLAUDE_PLANNER_VIA decides."""
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
    usage = getattr(response, "usage", None)
    fields = claude_usage_fields(usage)
    text = "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    record_usage(
        UsageRecord(
            "planner", "claude", claude_total_tokens(getattr(response, "usage", None))
        )
    )
    record_detail(
        UsageDetail(
            role="planner",
            runner="claude-api",
            provider="claude",
            model=ANTHROPIC_MODEL,
            attempt_id=current_attempt_id(),
            call_id=new_call_id(),
            input_tokens=(fields or {}).get("input_tokens"),
            output_tokens=(fields or {}).get("output_tokens"),
            cache_read_tokens=(fields or {}).get("cache_read_tokens"),
            cache_creation_tokens=(fields or {}).get("cache_creation_tokens"),
            total_tokens=(fields or {}).get("total_tokens"),
            output_chars=bounded_output_chars(text),
        )
    )
    if not text:
        raise RuntimeError("Claude returned no text for the plan. Please retry.")
    return text


def planner_message(
    prompt: str,
    provider: str | None,
    schema_name: str,
    max_tokens: int,
    *,
    context: ContextRequest | None = None,
) -> str:
    """Send a prompt to the selected planner and return its text response."""
    prompt = context_prompt(prompt, context or current_context.get())
    prompt += diagnostic_context()
    selected = normalize_planning_agent(provider)
    tool = get_tool(selected)
    if tool is None:
        raise ValueError(f"Unsupported planning agent: {selected}.")
    if selected == "claude":
        return _claude_planner_message(prompt, schema_name, max_tokens)
    if selected == "codex":
        return _codex_message(prompt, schema_name)
    return tool.planner(prompt, schema_name, max_tokens)
