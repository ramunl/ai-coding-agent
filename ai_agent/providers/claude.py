"""Claude API provider: the Anthropic API planner and its model dial.

Model selection, verification and the boot-env writer stay in
``ai_agent.model_manager``; this module wraps them into the provider-registry
contract and declares the planner adapter, auth, limits, errors, images,
structured-output and usage facts for the API tool (``claude``), never the
Claude Code CLI (``claude-code``).
"""

from __future__ import annotations

from ai_agent import model_manager
from ai_agent.config import ANTHROPIC_MODEL
from ai_agent.model_errors import is_model_not_found, model_error_message
from ai_agent.provider_registry import ErrorAdapter, ModelControl
from ai_agent.token_usage import UsageRecord, claude_total_tokens, record_usage
from ai_agent.tool_auth_providers import Reading, check

name = "claude"
enabled = True
roles = ("planner",)
images = True
structured_output = False

model = ModelControl(
    manageable=True,
    note="",
    current=model_manager.active_model,
    args=lambda: [],
    verify=model_manager.verify_model,
    list_models=model_manager.list_models,
    set=model_manager.set_model_in_env,
)


async def auth() -> Reading:
    """Observe the API credential state without making a request."""
    return await check("claude")


def limits() -> dict:
    """Return the last recorded Claude API rate-limit reading."""
    from ai_agent.provider_limits import limits_snapshot

    return limits_snapshot()["claude"]


errors = ErrorAdapter(
    classify=is_model_not_found,
    message=model_error_message,
)


def usage(reported: object) -> int | None:
    """Count input plus output tokens from an Anthropic usage object."""
    return claude_total_tokens(reported)


def _planner(prompt: str, schema_name: str, max_tokens: int) -> str:
    """Plan through the Anthropic API (the CLAUDE_PLANNER_VIA=api path).

    ``schema_name`` is accepted for adapter uniformity; the API path asks for
    text and does not pass a schema to the request.
    """
    from ai_agent.planning_provider import (
        WARNING,
        _create_message,
        attachments,
        blocks,
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
        if getattr(error, "status_code", None) == 429:
            raise RuntimeError("Claude image planning usage limit reached.") from None
        raise RuntimeError(
            "Claude image planning failed; retry or switch planner."
        ) from None
    record_usage(
        UsageRecord("planner", "claude", usage(getattr(response, "usage", None)))
    )
    text = "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not text:
        raise RuntimeError("Claude returned no text for the plan. Please retry.")
    return text


adapters = {"planner": _planner}
