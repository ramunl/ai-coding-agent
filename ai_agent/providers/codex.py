"""Codex provider: the Codex CLI planner and executor with a model override.

Model discovery and the CODEX_MODEL override stay in ``ai_agent.codex_models``;
this module wraps them into the provider-registry contract and declares the
planner/executor adapters, auth, limits, errors, images, structured-output and
usage facts.
"""

from __future__ import annotations

import os

from ai_agent import codex_models, model_manager
from ai_agent.model_errors import codex_capacity_message, is_codex_at_capacity
from ai_agent.provider_registry import ErrorAdapter, ModelControl
from ai_agent.token_usage import codex_total_tokens
from ai_agent.tool_auth_providers import Reading, check

name = "codex"
enabled = True
roles = ("planner", "executor")
images = True
structured_output = True


def _set_model(model: str) -> None:
    """Persist the override and apply it to future CLI invocations."""
    model_manager.set_model_in_env(model, "CODEX_MODEL")
    os.environ["CODEX_MODEL"] = model


model = ModelControl(
    manageable=True,
    note="Used for every Codex role. Changes apply to the next run.",
    current=codex_models.current_model,
    args=codex_models.model_args,
    verify=codex_models.verify_model,
    list_models=codex_models.list_models,
    set=_set_model,
)


async def auth() -> Reading:
    """Probe the Codex login status without running inference."""
    return await check("codex")


def limits() -> dict:
    """Return the last recorded Codex account-limit reading."""
    from ai_agent.provider_limits import limits_snapshot

    return limits_snapshot()["codex"]


errors = ErrorAdapter(
    classify=is_codex_at_capacity,
    message=codex_capacity_message,
)


def usage(output: str) -> int | None:
    """Return the total tokens Codex printed after ``tokens used``."""
    return codex_total_tokens(output)


def _planner(prompt: str, schema_name: str, max_tokens: int) -> str:
    """Plan with Codex through the shared planner dispatch."""
    from ai_agent.planning_provider import planner_message

    return planner_message(prompt, "codex", schema_name, max_tokens)


def _executor(prompt: str) -> object:
    """Implement with Codex through the shared executor dispatch."""
    from ai_agent.workflow import run_implementation_agent

    return run_implementation_agent(prompt, "codex")


adapters = {"planner": _planner, "executor": _executor}
