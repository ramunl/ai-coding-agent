"""Provider capability blocks for the Coding snapshot.

Maps each registry provider to a JSON-safe block the dashboard can render:
identity, label, role choices, status, model semantics, and the runner/adapter
identity behind the worker's tool names. Only declared capabilities are
published; anything a provider does not declare resolves to :data:`UNSUPPORTED`
and is never invented, guessed, or borrowed from another provider.

The worker's tool namespace maps ``claude`` to the Claude Code CLI *runner*
(``claude-code`` in the registry), never to the native-Anthropic API tool. The
snapshot keeps those identities separate so a runner routed through
token/DeepSeek is not presented as native Anthropic access. The same mapping
also drives the role validation used by ``set_planner``/``set_implementer``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ai_agent import provider_registry
from ai_agent.ai_tools import get_tool, manageable_tools, tools_for_role
from ai_agent.provider_availability import RUNNER_ALIASES

UNSUPPORTED = provider_registry.UNSUPPORTED

# Model IDs may carry a provider prefix with a slash (e.g. "deepseek/deepseek-chat"),
# but never a filesystem path or a shell command: alphanumeric-headed segments,
# only [A-Za-z0-9._:-] inside, no empty segments, no "..", and at most 100 bytes.
MODEL_PATTERN = re.compile(
    r"(?=.{1,100}\Z)(?!.*\.\.)"
    r"[A-Za-z0-9][A-Za-z0-9._:-]*(?:/[A-Za-z0-9][A-Za-z0-9._:-]*)*"
)

# Worker tool name -> provider identity; the inverse below shows which runner
# serves a provider. ``claude`` runs the Claude Code CLI, not the Anthropic API.
_PROVIDER_RUNNERS = {provider: tool for tool, provider in RUNNER_ALIASES.items()}

_ROLE_LABELS = {"planner": "planning agent", "executor": "implementation agent"}

# The user_data key holding an explicit per-role model override.
_ROLE_MODEL_KEYS = {"planner": "planner_model", "executor": "implementer_model"}


def valid_model_id(value: str) -> bool:
    """Whether a model id is safe to pass on: a slash-allowed id, never a path."""
    return bool(MODEL_PATTERN.fullmatch(value))


def _providers_for_tool(tool: str) -> frozenset[str]:
    """Registry provider names a user-facing tool may route to."""
    names = {tool}
    aliased = RUNNER_ALIASES.get(tool)
    if aliased:
        names.add(aliased)
    return frozenset(names)


def _enabled_for_role(tool: str, role: str) -> bool:
    """Whether an enabled registry provider serves ``role`` behind ``tool``."""
    return any(
        provider is not None
        and provider_registry.capability(provider, "enabled") is True
        and role in provider_registry.roles_of(provider)
        for name in _providers_for_tool(tool)
        for provider in (provider_registry.get_provider(name),)
    )


def enabled_tools_for_role(role: str) -> list[str]:
    """User-facing tool IDs backed by an enabled provider serving ``role``.

    Derived from the registry's roles plus enabled state, keeping the display
    order of the existing ai_tools role query.
    """
    return [tool for tool in tools_for_role(role) if _enabled_for_role(tool, role)]


def is_enabled_tool(tool: str) -> bool:
    """Whether the provider named ``tool`` is registered and enabled."""
    provider = provider_registry.get_provider(tool)
    return (
        provider is not None
        and provider_registry.capability(provider, "enabled") is True
    )


def enabled_manageable_tools() -> list[str]:
    """Manageable tool IDs whose registry provider is enabled."""
    return [tool for tool in manageable_tools() if is_enabled_tool(tool)]


def require_enabled_tool(value: str, role: str) -> str:
    """Normalize a tool choice and require an enabled provider serving ``role``.

    Raises ValueError with a message naming the allowed choices, mirroring the
    historical normalization errors.
    """
    selected = str(value).strip().lower()
    allowed = enabled_tools_for_role(role)
    if selected not in allowed:
        raise ValueError(
            f"Unsupported {_ROLE_LABELS[role]}: {selected}. Use {' or '.join(allowed)}."
        )
    return selected


def resolved_model(tool: str, role: str, user_data: Mapping | None = None) -> str:
    """The model a role will use for ``tool``: the persisted override, else the
    tool's current default model."""
    data = user_data or {}
    explicit = data.get(_ROLE_MODEL_KEYS[role])
    if explicit:
        return str(explicit)
    selected = get_tool(tool)
    if selected is not None:
        return str(selected.current_model())
    return ""


def _label(name: str) -> str:
    """A display label for a provider, preferring the ai_tools label."""
    tool = get_tool(name)
    if tool is not None and tool.label:
        return tool.label
    return name.replace("-", " ").title()


def _declared(value: Any) -> Any:
    """A JSON-safe form of a declared capability, or UNSUPPORTED."""
    if value is UNSUPPORTED or value is None:
        return UNSUPPORTED
    if callable(value):
        return "declared"
    return value


def _status(enabled: Any) -> str:
    if enabled is True:
        return "enabled"
    if enabled is False:
        return "disabled"
    return UNSUPPORTED


def model_semantics(provider: Any) -> Any:
    """The provider's model dial as JSON, or UNSUPPORTED when undeclared."""
    control = provider_registry.capability(provider, "model")
    if control is UNSUPPORTED:
        return UNSUPPORTED
    try:
        current = str(control.current())
    except Exception:
        current = ""
    return {
        "manageable": bool(getattr(control, "manageable", False)),
        "note": str(getattr(control, "note", "") or ""),
        "current": current,
    }


def adapter_roles(provider: Any) -> Any:
    """Roles with a wired adapter, or UNSUPPORTED when adapters are undeclared."""
    adapters = provider_registry.capability(provider, "adapters")
    if adapters is UNSUPPORTED:
        return UNSUPPORTED
    return sorted(role for role in adapters if callable(adapters.get(role)))


def runner_identity(name: str) -> str:
    """The worker-facing runner tool that serves provider ``name``."""
    return _PROVIDER_RUNNERS.get(name, name)


def provider_block(provider: Any) -> dict:
    """One provider's declared capabilities, JSON-safe."""
    name = getattr(provider, "name", "unknown")
    return {
        "id": name,
        "label": _label(name),
        "status": _status(provider_registry.capability(provider, "enabled")),
        "roles": list(provider_registry.roles_of(provider)),
        "model": model_semantics(provider),
        "adapter": adapter_roles(provider),
        "images": _declared(provider_registry.capability(provider, "images")),
        "structured_output": _declared(
            provider_registry.capability(provider, "structured_output")
        ),
        "usage": _declared(provider_registry.capability(provider, "usage")),
        "runner": runner_identity(name),
    }


def provider_blocks() -> list[dict]:
    """Every registered provider as a capability block, in discovery order."""
    providers = provider_registry.discover()
    return [provider_block(provider) for provider in providers.values()]
