"""The AI tool registry: one routing surface for every provider.

The per-tool contract (roles, adapters, model selection, auth, limits, error
classification, images, structured output, usage, enabled) lives in
``ai_agent/providers`` and is discovered by ``ai_agent.provider_registry``.
This module keeps the small ``AITool`` surface the /model command, dashboard
snapshot, planning, implementation, image delivery, limits, auth and usage
modules all route through, so adding a provider never means editing a central
dispatch list here.

Each ``AITool`` declares only what it can really do. Capabilities that a tool
does not declare fall back to safe defaults: no roles, no images, no auth, no
limits, unknown token counts.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ai_agent import codex_models, model_manager
from ai_agent.providers import claude, claude_code, codex, deepseek, opencode
from ai_agent.token_usage import codex_total_tokens


@dataclass(frozen=True)
class ModelInfo:
    """Describe a tool model and whether the agent can change it."""

    tool: str
    model: str
    manageable: bool  # can this agent change the model?
    note: str = ""  # where the model actually lives, when not manageable


def _unknown_tokens(_output: object) -> int | None:
    """The fail-safe token counter: the tool reported no count."""
    return None


def _auth_probe(name: str) -> Any:
    """Probe a tool's auth through the shared ToolAuth check.

    Resolved lazily so tests can patch ``ai_agent.tool_auth.check`` and the
    registry never imports the bot package at module level.
    """
    from ai_agent import tool_auth

    return tool_auth.check(name)


class AITool:
    """Base interface. Subclasses fill in what is real for that tool."""

    name: str
    label: str = ""
    manageable: bool = False
    # App-level agent roles this tool can serve ("planner" and/or "executor").
    roles: tuple[str, ...] = ()
    # Whether this tool accepts task-image attachments.
    images: bool = False
    # Stable ordering for agent pickers; unknown tools sort after built-ins.
    order: int = 50
    # Snapshot key under provider_limits.limits_snapshot(); None = no reading.
    limits_key: str | None = None
    # Async callable returning a tool_auth_providers.Reading; None = no auth.
    auth: Callable[..., Any] | None = None
    # Parser for implementation token totals; None = counts stay unknown.
    tokens: Callable[[object], int | None] | None = None

    def supports_role(self, role: str) -> bool:
        """Whether this tool can serve the given agent role."""
        return role in self.roles

    def current_model(self) -> str:
        """Return the model currently selected for this tool."""
        raise NotImplementedError

    def set_model(self, model: str) -> None:
        """Persist a verified model selection for this tool."""
        raise NotImplementedError

    def verify(self, model: str) -> tuple[bool, str]:
        """Check model availability and return a status with explanatory text."""
        raise NotImplementedError

    def list_models(self) -> tuple[bool, list[dict[str, str]] | str]:
        """Return available model metadata or a failure explanation."""
        raise NotImplementedError

    def info(self) -> ModelInfo:
        """Describe the current model and its configuration capabilities."""
        return ModelInfo(self.name, self.current_model(), self.manageable, self._note())

    def _note(self) -> str:
        return ""

    def planner(self, prompt: str, schema_name: str, max_tokens: int) -> str:
        """Plan a prompt through this tool and return its text reply."""
        raise NotImplementedError(f"{self.name} has no planner")

    def command(self, prompt: str) -> list[str]:
        """Build the implementation command for this tool."""
        raise NotImplementedError(f"{self.name} has no implementation command")

    def image_run(
        self,
        command: list[str],
        run: Callable[..., Any],
        *,
        images: list[tuple[str, bytes]],
        missing: int,
        **kwargs: Any,
    ) -> Any:
        """Run a command with native image input for this tool."""
        raise NotImplementedError(f"{self.name} does not support image input")

    def limits(self) -> dict:
        """Return the last recorded limits reading for this tool."""
        return {}


class ClaudeApiTool(AITool):
    """The Claude brand: API planning, and Claude Code CLI implementation."""

    name = claude.name
    label = "Claude"
    manageable = claude.model.manageable
    roles = ("planner", "executor")
    images = True
    order = 1
    limits_key = "claude"
    auth = functools.partial(_auth_probe, "claude")
    tokens = staticmethod(_unknown_tokens)

    def current_model(self) -> str:
        """Return the model currently selected for this tool."""
        return model_manager.active_model()

    def set_model(self, model: str) -> None:
        """Persist a verified model selection for this tool."""
        model_manager.set_model_in_env(model)

    def verify(self, model: str) -> tuple[bool, str]:
        """Check model availability and return a status with explanatory text."""
        return model_manager.verify_model(model)

    def list_models(self) -> tuple[bool, list[dict[str, str]] | str]:
        """Return available model metadata or a failure explanation."""
        return model_manager.list_models()

    def _note(self) -> str:
        return claude.model.note

    def planner(self, prompt: str, schema_name: str, max_tokens: int) -> str:
        """Plan with the API or Claude Code, as CLAUDE_PLANNER_VIA decides."""
        from ai_agent.planning_provider import _claude_planner_message

        return _claude_planner_message(prompt, schema_name, max_tokens)

    def command(self, prompt: str) -> list[str]:
        """Build the Claude Code CLI implementation command.

        The stream-json output format makes tokens/provider/model observable;
        ``workflow`` parses the stream back into prose for the owner.
        """
        from ai_agent.claude_code_usage import claude_code_stream_args
        from ai_agent.workflow import (
            claude_allowed_tools_args,
            claude_context_control_args,
            safe_claude_code_args,
        )

        return [
            "claude",
            "-p",
            prompt,
            *safe_claude_code_args(),
            *claude_allowed_tools_args(),
            *claude_code_stream_args(),
            *claude_context_control_args(),
        ]

    def image_run(
        self,
        command: list[str],
        run: Callable[..., Any],
        *,
        images: list[tuple[str, bytes]],
        missing: int,
        **kwargs: Any,
    ) -> Any:
        """Deliver images to Claude Code through the stream-json envelope."""
        from ai_agent.image_delivery import _claude_image_run

        return _claude_image_run(command, run, images=images, missing=missing, **kwargs)

    def limits(self) -> dict:
        """Return the last recorded Claude API rate-limit reading."""
        from ai_agent.provider_limits import _claude_reading

        return _claude_reading()


class CodexTool(AITool):
    """The Codex CLI planner and executor, with a model override."""

    name = codex.name
    label = "Codex"
    manageable = codex.model.manageable
    roles = ("planner", "executor")
    images = True
    order = 0
    limits_key = "codex"
    auth = functools.partial(_auth_probe, "codex")
    tokens = staticmethod(codex_total_tokens)

    def current_model(self) -> str:
        """Return the override, or the explicit CLI-default option."""
        return codex_models.current_model()

    def list_models(self) -> tuple[bool, list[dict[str, str]] | str]:
        """Read picker-visible models without running inference."""
        return codex_models.list_models()

    def verify(self, model: str) -> tuple[bool, str]:
        """Require a model from a fresh catalog or the default option."""
        return codex_models.verify_model(model)

    def set_model(self, model: str) -> None:
        """Persist the override and apply it to future CLI invocations."""
        model_manager.set_model_in_env(model, "CODEX_MODEL")
        os.environ["CODEX_MODEL"] = model

    def _note(self) -> str:
        return codex.model.note

    def planner(self, prompt: str, schema_name: str, max_tokens: int) -> str:
        """Plan with Codex through the shared structured-output path."""
        from ai_agent.planning_provider import _codex_message

        return _codex_message(prompt, schema_name)

    def command(self, prompt: str) -> list[str]:
        """Build the Codex implementation command with workspace-write sandbox."""
        from ai_agent.workflow import codex_context_control_args

        return [
            "codex",
            "exec",
            *codex_models.model_args(),
            "-s",
            "workspace-write",
            *codex_context_control_args(),
            prompt,
        ]

    def image_run(
        self,
        command: list[str],
        run: Callable[..., Any],
        *,
        images: list[tuple[str, bytes]],
        missing: int,
        **kwargs: Any,
    ) -> Any:
        """Deliver images to Codex through RAM-backed --image flags."""
        from ai_agent.image_delivery import _codex_image_run

        return _codex_image_run(command, run, images=images, missing=missing, **kwargs)

    def limits(self) -> dict:
        """Return the last recorded Codex account-limit reading."""
        from ai_agent.provider_limits import _codex_reading

        return _codex_reading()


class CliTool(AITool):
    """A CLI whose model lives in its own config; read-only from here."""

    manageable = False

    def __init__(self, name: str, env_var: str, default: str, note: str) -> None:
        """Configure the CLI's model display and configuration guidance."""
        self.name = name
        self._env_var = env_var
        self._default = default
        self._note_text = note

    def current_model(self) -> str:
        """Return the model currently selected for this tool."""
        # Best-effort display only: some setups pin the CLI model via env.
        return os.environ.get(self._env_var, self._default)

    def _note(self) -> str:
        return self._note_text


class ClaudeCodeTool(CliTool):
    """The Claude Code CLI's read-only model dial, auth and limits."""

    label = "Claude Code"
    roles = ()
    images = False
    order = 2
    limits_key = "claude_code"
    auth = functools.partial(_auth_probe, "claude-code")

    def limits(self) -> dict:
        """Return the last recorded Claude Code subscription reading."""
        from ai_agent.provider_limits import _claude_code_reading

        return _claude_code_reading()


class DeepSeekTool(AITool):
    """The native DeepSeek API planner; read-only model from DEEPSEEK_MODEL."""

    name = deepseek.name
    label = "DeepSeek"
    manageable = deepseek.model.manageable
    roles = ("planner",)
    images = False
    order = 3
    limits_key = "deepseek"
    auth = functools.partial(_auth_probe, "deepseek")

    def current_model(self) -> str:
        """Return the configured DeepSeek planner model."""
        return deepseek.model.current()

    def verify(self, model: str) -> tuple[bool, str]:
        """Verify a model against the provider catalog without inference."""
        return deepseek.model.verify(model)

    def list_models(self) -> tuple[bool, list[dict[str, str]] | str]:
        """List DeepSeek models through the non-inference catalog endpoint."""
        return deepseek.model.list_models()

    def _note(self) -> str:
        return deepseek.model.note

    def planner(self, prompt: str, schema_name: str, max_tokens: int) -> str:
        """Plan with DeepSeek through the native HTTPS Chat Completions API."""
        from ai_agent.deepseek_planner import planner_message

        return planner_message(prompt, schema_name, max_tokens)

    def limits(self) -> dict:
        """Return the last recorded DeepSeek balance reading."""
        from ai_agent.provider_limits import _deepseek_reading

        return _deepseek_reading()


class OpenCodeTool(CliTool):
    """The OpenCode CLI executor; read-only DeepSeek model, owner-gated.

    ``roles`` is set per instance from the provider's ``enabled`` flag, so the
    disabled provider stays out of ``tools_for_role("executor")`` and the worker
    can never route through it until the owner flips the gate.
    """

    label = "OpenCode"
    images = False
    order = 4
    limits_key = "deepseek"
    auth = functools.partial(_auth_probe, "opencode")
    tokens = staticmethod(opencode.usage)

    def __init__(self) -> None:
        """Configure the read-only dial and the owner-gated executor role."""
        super().__init__(
            opencode.name,
            env_var=opencode.MODEL_ENV_VAR,
            default=opencode.MODEL_DEFAULT,
            note=opencode.model.note,
        )
        self.roles = ("executor",) if opencode.enabled else ()

    def command(self, prompt: str) -> list[str]:
        """Build the OpenCode implementation command with JSON event output."""
        return [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            opencode.model.current(),
            prompt,
        ]

    def limits(self) -> dict:
        """Return the last recorded DeepSeek balance reading (OpenCode's backend)."""
        from ai_agent.provider_limits import _deepseek_reading

        return _deepseek_reading()


class ProviderTool(AITool):
    """A generic tool adapted from one provider module's declared capabilities.

    The built-in tools stay hand-written so their exact CLI arguments and order
    never regress; a *new* provider module is instead adapted here from the
    provider registry. Roles, model, auth, limits, errors, images and usage come
    from the provider's declared capabilities, and the planner plus the generic
    executor ``command``/``image_run`` sub-adapters come from its ``adapters``
    mapping. A disabled provider keeps no roles, so it never routes.
    """

    def __init__(self, provider: Any) -> None:
        """Adapt one provider module's declared capabilities to the tool surface."""
        from ai_agent import provider_registry

        self._provider = provider
        self.name = str(getattr(provider, "name", "unknown"))
        self.label = self.name.replace("-", " ").title()
        self.order = 50

        enabled = provider_registry.capability(provider, "enabled") is True
        roles = provider_registry.roles_of(provider)
        self.roles = roles if enabled else ()

        images = provider_registry.capability(provider, "images")
        self.images = images is True

        model = provider_registry.capability(provider, "model")
        self._model = model if model is not provider_registry.UNSUPPORTED else None
        self.manageable = (
            bool(getattr(self._model, "manageable", False))
            if self._model is not None
            else False
        )

        auth = provider_registry.capability(provider, "auth")
        self.auth = auth if callable(auth) else None

        limits = provider_registry.capability(provider, "limits")
        self._limits = limits if callable(limits) else None
        self.limits_key = self.name if self._limits is not None else None

        usage = provider_registry.capability(provider, "usage")
        self.tokens = usage if callable(usage) else None

        errors = provider_registry.capability(provider, "errors")
        self.errors = errors if errors is not provider_registry.UNSUPPORTED else None

        adapters = provider_registry.capability(provider, "adapters")
        self._adapters = adapters if isinstance(adapters, Mapping) else {}

    def current_model(self) -> str:
        """Return the provider's currently selected model, or '' when unknown."""
        if self._model is None:
            return ""
        try:
            return str(self._model.current())
        except Exception:
            return ""

    def set_model(self, model: str) -> None:
        """Persist a verified model selection through the provider's dial."""
        if self._model is None or not self.manageable or self._model.set is None:
            raise NotImplementedError(f"{self.name} has no model override")
        self._model.set(model)

    def verify(self, model: str) -> tuple[bool, str]:
        """Check model availability through the provider's catalog."""
        if self._model is None:
            return False, f"{self.name} has no model catalog"
        return self._model.verify(model)

    def list_models(self) -> tuple[bool, list[dict[str, str]] | str]:
        """List provider models through its catalog adapter."""
        if self._model is None:
            return False, f"{self.name} has no model catalog"
        return self._model.list_models()

    def _note(self) -> str:
        return str(getattr(self._model, "note", "") or "") if self._model else ""

    def planner(self, prompt: str, schema_name: str, max_tokens: int) -> str:
        """Plan through the provider's declared planner adapter."""
        adapter = self._adapters.get("planner")
        if not callable(adapter):
            raise NotImplementedError(f"{self.name} has no planner adapter")
        return adapter(prompt, schema_name, max_tokens)

    def command(self, prompt: str) -> list[str]:
        """Build the implementation command through the provider's builder."""
        adapter = self._adapters.get("command")
        if not callable(adapter):
            raise NotImplementedError(f"{self.name} has no command adapter")
        return list(adapter(prompt))

    def image_run(
        self,
        command: list[str],
        run: Callable[..., Any],
        *,
        images: list[tuple[str, bytes]],
        missing: int,
        **kwargs: Any,
    ) -> Any:
        """Deliver images through the provider's image adapter."""
        adapter = self._adapters.get("image_run")
        if not callable(adapter):
            raise NotImplementedError(f"{self.name} does not support image input")
        return adapter(command, run, images=images, missing=missing, **kwargs)

    def limits(self) -> dict:
        """Return the provider's last recorded limits reading."""
        if self._limits is None:
            return {}
        try:
            value = self._limits()
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def explain_error(self, error: BaseException) -> str | None:
        """The provider's own explanation for a recognized error, or None.

        The new-provider error contract uses single-argument ``classify`` and
        ``message`` adapters; a provider that declares a different signature is
        surfaced as-is and never guessed at.
        """
        adapter = self.errors
        if adapter is None:
            return None
        try:
            if adapter.classify(error):
                return str(adapter.message(error))
        except Exception:
            return None
        return None


_TOOLS: dict[str, AITool] = {
    "claude": ClaudeApiTool(),
    "codex": CodexTool(),
    "claude-code": ClaudeCodeTool(
        claude_code.name,
        env_var=claude_code.MODEL_ENV_VAR,
        default=claude_code.MODEL_DEFAULT,
        note=claude_code.model.note,
    ),
    "deepseek": DeepSeekTool(),
    "opencode": OpenCodeTool(),
}


def register_tool(name: str, tool: AITool) -> None:
    """Register a tool under a normalized name; used by tests and providers."""
    _TOOLS[name.strip().lower()] = tool


def unregister_tool(name: str) -> None:
    """Forget a registered tool; used by tests to restore the registry."""
    _TOOLS.pop(name.strip().lower(), None)


def install_provider_tools() -> list[str]:
    """Register a generic tool for every discovered provider, idempotently.

    A provider already served by a hand-written tool is never replaced, so the
    built-in CLI arguments and order stay intact. A provider served by an
    earlier ``ProviderTool`` is rebuilt from its current declarations, so a
    disabled provider loses its roles. Returns the installed tool names.
    """
    from ai_agent import provider_registry

    installed: list[str] = []
    for name, provider in provider_registry.discover().items():
        existing = _TOOLS.get(name)
        if existing is not None and not isinstance(existing, ProviderTool):
            continue
        register_tool(name, ProviderTool(provider))
        installed.append(name)
    return installed


_provider_tools_installed = False


def _ensure_provider_tools() -> None:
    """Install provider tools once, lazily, without a module-level import cycle.

    The provider registry is imported inside the function so ``ai_tools`` never
    imports ``provider_capabilities`` (which imports this module) at import time.
    """
    global _provider_tools_installed
    if _provider_tools_installed:
        return
    install_provider_tools()
    _provider_tools_installed = True


def all_tools() -> dict[str, AITool]:
    """Return a copy of the registered tools keyed by name."""
    _ensure_provider_tools()
    return dict(_TOOLS)


def tools_for_role(role: str) -> list[str]:
    """Return registered tool names that serve ``role``, in display order."""
    _ensure_provider_tools()
    return [
        tool.name
        for tool in sorted(
            (tool for tool in _TOOLS.values() if role in tool.roles),
            key=lambda tool: (tool.order, tool.name),
        )
    ]


def manageable_tools() -> list[str]:
    """Return the names of tools whose model this agent can switch."""
    _ensure_provider_tools()
    return [tool.name for tool in _TOOLS.values() if tool.manageable]


def auth_tools() -> list[AITool]:
    """Return tools that declare an auth observation, in display order."""
    _ensure_provider_tools()
    return sorted(
        (tool for tool in _TOOLS.values() if tool.auth is not None),
        key=lambda tool: (tool.order, tool.name),
    )


def limits_tools() -> list[AITool]:
    """Return tools that declare a limits snapshot key, in display order."""
    _ensure_provider_tools()
    return sorted(
        (tool for tool in _TOOLS.values() if tool.limits_key is not None),
        key=lambda tool: (tool.order, tool.name),
    )


def token_tools() -> list[str]:
    """Return usage-capable tools, including those with unknown token counts."""
    _ensure_provider_tools()
    return [
        tool.name
        for tool in sorted(
            (tool for tool in _TOOLS.values() if tool.roles),
            key=lambda tool: (tool.order, tool.name),
        )
    ]


def known_tools() -> list[str]:
    """Return the registered tool names in display order."""
    _ensure_provider_tools()
    return list(_TOOLS)


def get_tool(name: str) -> AITool | None:
    """Resolve a case-insensitive tool name, or return None if unknown."""
    _ensure_provider_tools()
    return _TOOLS.get(name.strip().lower())


def all_info() -> list[ModelInfo]:
    """Describe the current model for every registered tool."""
    _ensure_provider_tools()
    return [tool.info() for tool in _TOOLS.values()]
