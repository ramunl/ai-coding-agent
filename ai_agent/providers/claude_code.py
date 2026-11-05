"""Claude Code CLI provider: the subscription planner and executor.

Claude Code owns its login and model; the /model dial is read-only here.
Planning runs through ``ai_agent.claude_code_planner`` and implementation
through the shared executor with the ``claude`` agent name. This is the
``claude-code`` tool, distinct from the ``claude`` API tool.
"""

from __future__ import annotations

import os

from ai_agent.config import CLAUDE_PLANNER_MODEL
from ai_agent.provider_registry import ModelControl
from ai_agent.tool_auth_providers import Reading, check

name = "claude-code"
enabled = True
roles = ("planner", "executor")
images = True
structured_output = True

MODEL_ENV_VAR = "CLAUDE_CODE_MODEL"
MODEL_DEFAULT = "(claude CLI default)"
NOTE = "Managed by the Claude CLI's own login/config, not this agent."


def _current_model() -> str:
    """Return the CLI display model, or its own default when unset."""
    return os.environ.get(MODEL_ENV_VAR, MODEL_DEFAULT)


def _model_args() -> list[str]:
    """Pass CLAUDE_PLANNER_MODEL explicitly when one is configured."""
    return ["--model", CLAUDE_PLANNER_MODEL] if CLAUDE_PLANNER_MODEL else []


def _read_only_verify(model: str) -> tuple[bool, str]:
    """Reject switching: the Claude CLI owns this model."""
    return False, NOTE


def _read_only_list() -> tuple[bool, str]:
    """Report that models are not listable from here."""
    return False, NOTE


model = ModelControl(
    manageable=False,
    note=NOTE,
    current=_current_model,
    args=_model_args,
    verify=_read_only_verify,
    list_models=_read_only_list,
    set=None,
)


async def auth() -> Reading:
    """Probe the Claude Code login status without running inference."""
    return await check("claude-code")


def limits() -> dict:
    """Return the last recorded Claude Code subscription reading."""
    from ai_agent.provider_limits import limits_snapshot

    return limits_snapshot()["claude_code"]


def _planner(prompt: str, schema_name: str, max_tokens: int) -> str:
    """Plan with Claude Code on the subscription."""
    from ai_agent.claude_code_planner import claude_code_message
    from ai_agent.planning_provider import schema_path

    return claude_code_message(prompt, schema_path(schema_name))


def _executor(prompt: str) -> object:
    """Implement with Claude Code through the shared executor dispatch."""
    from ai_agent.workflow import run_implementation_agent

    return run_implementation_agent(prompt, "claude")


adapters = {"planner": _planner, "executor": _executor}
