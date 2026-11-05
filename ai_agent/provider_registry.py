"""Provider registry: one capability contract per AI provider.

Every provider lives in ``ai_agent/providers/<name>.py`` and declares the same
surface — roles, adapters, model selection, auth, limits, error
classification, images, structured output, usage and enabled state. The
registry discovers those modules and answers capability questions. A
capability a provider does not declare resolves to :data:`UNSUPPORTED`: it is
never invented, guessed, or borrowed from another provider.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

PACKAGE = "ai_agent.providers"
PROVIDER_MODULES = ("claude", "codex", "claude_code", "opencode", "deepseek")

# The canonical capability names a provider may declare. Names outside this
# list resolve to UNSUPPORTED, so a typo fails closed instead of being guessed.
CAPABILITIES = (
    "roles",
    "adapters",
    "model",
    "auth",
    "limits",
    "errors",
    "images",
    "structured_output",
    "usage",
    "enabled",
)

UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ModelControl:
    """The model dial a provider exposes to the /model command."""

    manageable: bool
    note: str
    current: Callable[[], str]
    args: Callable[[], list[str]]
    verify: Callable[[str], tuple[bool, str]]
    list_models: Callable[[], tuple[bool, list[dict[str, str]] | str]]
    set: Callable[[str], None] | None


@dataclass(frozen=True)
class ErrorAdapter:
    """Provider-specific error recognition and explanation."""

    classify: Callable[..., bool]
    message: Callable[..., str]


class Provider(Protocol):
    """The capability surface a provider module may declare.

    ``name`` and ``enabled`` are always present. Every other attribute is
    optional; a missing attribute is reported as :data:`UNSUPPORTED` by
    :func:`capability`. Disabled providers keep ``enabled=False`` and declare
    nothing else until their capabilities are verified.
    """

    name: str
    enabled: bool

    roles: tuple[str, ...]
    adapters: Mapping[str, Callable[..., object]]
    model: ModelControl
    auth: Callable[..., object]
    limits: Callable[[], dict]
    errors: ErrorAdapter
    images: bool
    structured_output: bool
    usage: Callable[..., int | None]


_providers: dict[str, Any] = {}
_discovered = False


def register(name: str, provider: object) -> None:
    """Register a provider object under a normalized name."""
    _providers[name.strip().lower()] = provider


def discover() -> dict[str, Any]:
    """Import every known provider module and register it.

    A module that cannot be imported is left unregistered: an unimportable
    provider is unsupported, exactly like an unknown name.
    """
    global _discovered
    for module_name in PROVIDER_MODULES:
        try:
            module = importlib.import_module(f"{PACKAGE}.{module_name}")
        except ImportError:
            continue
        register(getattr(module, "name", module_name), module)
    _discovered = True
    return dict(_providers)


def reset() -> None:
    """Forget registered providers; used by tests and reloads."""
    global _discovered
    _providers.clear()
    _discovered = False


def _ensure_discovered() -> None:
    if not _discovered:
        discover()


def get_provider(name: str) -> Any | None:
    """Resolve a provider by case-insensitive name, or None when unknown."""
    _ensure_discovered()
    return _providers.get(name.strip().lower())


def known_providers() -> list[str]:
    """Return registered provider names in discovery order."""
    _ensure_discovered()
    return list(_providers)


def enabled_providers() -> list[Any]:
    """Return only providers whose ``enabled`` flag is truthy."""
    _ensure_discovered()
    return [
        provider
        for provider in _providers.values()
        if getattr(provider, "enabled", False)
    ]


def capability(provider: object, name: str) -> Any:
    """Return a declared capability, or UNSUPPORTED when it is absent.

    A declared-but-false boolean (for example ``enabled=False``) is returned as
    False, not as UNSUPPORTED: only *undeclared* capabilities are unsupported.
    """
    if name not in CAPABILITIES:
        return UNSUPPORTED
    value = getattr(provider, name, None)
    return UNSUPPORTED if value is None else value
