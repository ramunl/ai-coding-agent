"""Provider registry: one capability contract per AI provider.

Every provider lives in a module listed in the owner-approved manifest
(``ai_agent/providers/manifest.json``) and declares the same surface — roles,
adapters, model selection, auth, limits, error classification, images,
structured output, usage and enabled state. The registry imports only the
modules that manifest allows, so an unlisted module is never imported, and
answers capability questions from what each module declares. A capability a
provider does not declare resolves to :data:`UNSUPPORTED`: it is never
invented, guessed, or borrowed from another provider.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

PACKAGE = "ai_agent.providers"
# Fallback default used only when no approved manifest is present on disk.
PROVIDER_MODULES = ("claude", "codex", "claude_code", "opencode", "deepseek")

# The approved allow-list, read from the checkout next to the providers package.
# Deployment is a git checkout + restart, so no wheel package-data config is
# needed: the file is always present in the checkout.
MANIFEST_PATH = Path(__file__).with_name("providers") / "manifest.json"
MANIFEST_ENV = "AI_AGENT_PROVIDER_MANIFEST"

# A module path is a chain of Python identifiers joined by dots. Slashes,
# dashes, spaces and shell metacharacters are rejected, so a manifest entry can
# never import a filesystem path or an arbitrary module.
_MODULE_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

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
class ManifestEntry:
    """One approved provider module and its secret-free maintenance metadata."""

    module: str
    package: str = ""
    pinned_version: str = ""
    gated: bool = False


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

    ``adapters`` maps role names (``planner``/``executor``) to role adapters,
    and may also declare the generic executor sub-adapters ``command`` (a
    command builder returning ``list[str]``) and ``image_run`` (image delivery)
    that :class:`ai_agent.ai_tools.ProviderTool` routes through.
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
_maintenance: dict[str, dict[str, Any]] = {}
_discovered = False


def _parse_manifest(text: str) -> list[ManifestEntry]:
    """Parse an owner-approved manifest, failing closed on any bad entry.

    Every entry must be an object with a dotted module path. Non-dotted module
    paths, duplicate modules and malformed metadata raise ``ValueError`` so a
    bad allow-list can never cause an arbitrary import.
    """
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ValueError(f"provider manifest is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("provider manifest must be a JSON object")
    raw_entries = data.get("providers")
    if not isinstance(raw_entries, list):
        raise ValueError("provider manifest 'providers' must be a list")
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"provider manifest entry {index} is not an object")
        module = entry.get("module")
        if not isinstance(module, str) or not _MODULE_ID.fullmatch(module):
            raise ValueError(
                f"provider manifest entry {index} has a non-dotted module path"
            )
        if module in seen:
            raise ValueError(f"provider manifest lists {module} more than once")
        seen.add(module)
        for key in ("package", "pinned_version"):
            if not isinstance(entry.get(key, ""), str):
                raise ValueError(
                    f"provider manifest entry {module} has a non-string {key}"
                )
        if not isinstance(entry.get("gated", False), bool):
            raise ValueError(
                f"provider manifest entry {module} has a non-boolean gated flag"
            )
        entries.append(
            ManifestEntry(
                module=module,
                package=entry.get("package", ""),
                pinned_version=entry.get("pinned_version", ""),
                gated=entry.get("gated", False),
            )
        )
    return entries


def _manifest_entries() -> tuple[list[ManifestEntry], bool]:
    """Approved manifest entries and whether they came from a real manifest.

    A missing or unreadable manifest falls back to the built-in approved names
    so a checkout always discovers the known providers; a present-but-malformed
    manifest fails closed by raising.
    """
    path = os.environ.get(MANIFEST_ENV, str(MANIFEST_PATH))
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return [
            ManifestEntry(module=f"{PACKAGE}.{name}") for name in PROVIDER_MODULES
        ], False
    return _parse_manifest(text), True


def register(name: str, provider: object) -> None:
    """Register a provider object under a normalized name."""
    _providers[name.strip().lower()] = provider


def discover() -> dict[str, Any]:
    """Import the approved provider modules and register them.

    Only modules listed in the manifest (or the built-in fallback when the
    manifest is absent) are imported. A module that fails to import is left
    unregistered, exactly like an unknown name. A malformed manifest fails
    closed by raising, so no entry can ever import an arbitrary module.
    """
    global _discovered
    entries, from_manifest = _manifest_entries()
    for entry in entries:
        try:
            module = importlib.import_module(entry.module)
        except ImportError:
            continue
        name = getattr(module, "name", entry.module.rsplit(".", 1)[-1])
        register(name, module)
        if from_manifest:
            _maintenance[name.strip().lower()] = {
                "package": entry.package,
                "pinned_version": entry.pinned_version,
                "gated": entry.gated,
            }
    _discovered = True
    return dict(_providers)


def reset() -> None:
    """Forget registered providers; used by tests and reloads."""
    global _discovered
    _providers.clear()
    _maintenance.clear()
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


def maintenance(name: str) -> dict[str, Any] | None:
    """Manifest maintenance metadata for a provider, or None when undeclared.

    The metadata is secret-free (``package``, ``pinned_version``, ``gated``)
    and only exists for providers discovered from a real manifest, so the
    snapshot can publish it without leaking credentials and stays backward
    compatible when a provider has none.
    """
    _ensure_discovered()
    return _maintenance.get(name.strip().lower())


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


def roles_of(provider: object) -> tuple[str, ...]:
    """The roles a provider declares, or the empty tuple when unsupported."""
    value = capability(provider, "roles")
    return value if isinstance(value, tuple) else ()


def supports_role(provider: object, role: str) -> bool:
    """Whether provider declares it can serve role."""
    return role in roles_of(provider)


def providers_for_role(role: str) -> list[Any]:
    """Enabled providers that declare ``role``, in discovery order."""
    _ensure_discovered()
    return [
        provider
        for provider in _providers.values()
        if getattr(provider, "enabled", False) and role in roles_of(provider)
    ]


def names_for_role(role: str) -> list[str]:
    """Names of the enabled providers serving ``role``, in discovery order."""
    return [provider.name for provider in providers_for_role(role)]
