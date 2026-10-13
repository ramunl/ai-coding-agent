"""Send planning prompts to the selected provider and return its text output.

Importing this module has no side effects: the Anthropic client is created on
first use and configuration is read when a request is made.
"""

from __future__ import annotations

import functools
import os
import tempfile
from pathlib import Path

import anthropic

from ai_agent.codex_models import model_args
from ai_agent.model_errors import codex_capacity_explained, model_error_message
from ai_agent.ops_context import diagnostic_context
from ai_agent.provider_limits import record_claude_response
from ai_agent.shell import run

SCHEMAS_DIR = Path(__file__).parent / "schemas"
SUPPORTED_PLANNING_AGENTS = ("codex", "claude")


def schema_path(schema_name: str) -> Path:
    """Resolve a structured-output schema bundled with the package."""
    return SCHEMAS_DIR / schema_name


@functools.cache
def client() -> anthropic.Anthropic:
    """Create the Anthropic client once, on the first Claude planning request."""
    from ai_agent.config import ANTHROPIC_KEY

    return anthropic.Anthropic(
        api_key=ANTHROPIC_KEY,
        http_client=anthropic.DefaultHttpxClient(
            event_hooks={"response": [record_claude_response]}
        ),
    )


def normalize_planning_agent(agent: str | None = None) -> str:
    """Resolve the requested planner or reject an unsupported provider."""
    from ai_agent.config import PLANNING_AGENT

    value = (agent or PLANNING_AGENT).strip().lower()
    if value not in SUPPORTED_PLANNING_AGENTS:
        raise ValueError(f"Unsupported planning agent: {value}. Use codex or claude.")
    return value


def planning_agent_label(agent: str | None = None) -> str:
    """Return the display name for the selected planner."""
    return {"codex": "Codex", "claude": "Claude"}[normalize_planning_agent(agent)]


def _codex_message(prompt: str, schema_name: str) -> str:
    from ai_agent.config import CODEX_TIMEOUT_SECONDS

    with tempfile.NamedTemporaryFile(
        prefix="ai-agent-codex-", suffix=".json"
    ) as output:
        with codex_capacity_explained():
            run(
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
                timeout=CODEX_TIMEOUT_SECONDS,
            )
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
    prompt: str, provider: str | None, schema_name: str, max_tokens: int
) -> str:
    """Send a prompt to the selected planner and return its text response."""
    from ai_agent.config import ANTHROPIC_MODEL

    prompt += diagnostic_context()
    selected = normalize_planning_agent(provider)
    if selected == "codex":
        return _codex_message(prompt, schema_name)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Claude planning is unavailable because "
            "ANTHROPIC_API_KEY is not configured. "
            "Use /planner codex or configure the key."
        )
    response = _create_message(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not text:
        raise RuntimeError("Claude returned no text for the plan. Please retry.")
    return text
