"""DeepSeek provider: the native DeepSeek API planner.

Planning runs through ``ai_agent.deepseek_planner`` (native HTTPS Chat
Completions with JSON-object output). The model dial is read-only from this
agent: ``DEEPSEEK_MODEL`` is the configured default and ``DEEPSEEK_FLASH_MODEL``
the cheaper option, so they are never switched by the /model command. Model and
balance discovery use non-inference GETs that never consume quota.
"""

from __future__ import annotations

from ai_agent import deepseek_planner
from ai_agent.config import DEEPSEEK_MODEL
from ai_agent.provider_registry import ErrorAdapter, ModelControl
from ai_agent.tool_auth_providers import Reading, check

name = "deepseek"
enabled = True
roles = ("planner",)
images = False
structured_output = True

NOTE = "Set by DEEPSEEK_MODEL/DEEPSEEK_FLASH_MODEL; not switched by this agent."


def _current_model() -> str:
    """Return the configured DeepSeek planner model."""
    return DEEPSEEK_MODEL


def _verify(model: str) -> tuple[bool, str]:
    """Verify a model id against the provider catalog without inference."""
    try:
        models = deepseek_planner.discover_models()
    except deepseek_planner.DeepSeekError as error:
        return False, str(error)
    if model in {entry["id"] for entry in models}:
        return True, ""
    return False, f"Unknown DeepSeek model: {model}"


def _list_models() -> tuple[bool, list[dict[str, str]] | str]:
    """List provider models through the non-inference catalog endpoint."""
    try:
        return True, deepseek_planner.discover_models()
    except deepseek_planner.DeepSeekError as error:
        return False, str(error)


model = ModelControl(
    manageable=False,
    note=NOTE,
    current=_current_model,
    args=lambda: [],
    verify=_verify,
    list_models=_list_models,
    set=None,
)


async def auth() -> Reading:
    """Observe the API-key presence without making a request."""
    return await check("deepseek")


def limits() -> dict:
    """Return the last recorded DeepSeek balance reading."""
    from ai_agent.provider_limits import limits_snapshot

    return limits_snapshot()["deepseek"]


def _is_deepseek_error(error: BaseException) -> bool:
    """Whether an exception is a classified DeepSeek planner failure."""
    return isinstance(error, deepseek_planner.DeepSeekError)


def _error_message(error: BaseException) -> str:
    """A secret-free explanation for a DeepSeek planner failure."""
    return str(error) if str(error) else "DeepSeek planning failed."


errors = ErrorAdapter(classify=_is_deepseek_error, message=_error_message)


def usage(reported: object) -> int | None:
    """Count input plus output tokens from a DeepSeek usage object."""
    return deepseek_planner.deepseek_total_tokens(reported)


def _planner(prompt: str, schema_name: str, max_tokens: int) -> str:
    """Plan with DeepSeek through the native API planner."""
    return deepseek_planner.planner_message(prompt, schema_name, max_tokens)


adapters = {"planner": _planner}
