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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ai_agent import codex_models, model_manager
from ai_agent.providers import claude, claude_code, codex
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
        """Build the Claude Code CLI implementation command."""
        from ai_agent.workflow import claude_allowed_tools_args, safe_claude_code_args

        return [
            "claude",
            "-p",
            prompt,
            *safe_claude_code_args(),
            *claude_allowed_tools_args(),
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
        return [
            "codex",
            "exec",
            *codex_models.model_args(),
            "-s",
            "workspace-write",
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


_TOOLS: dict[str, AITool] = {
    "claude": ClaudeApiTool(),
    "codex": CodexTool(),
    "claude-code": ClaudeCodeTool(
        claude_code.name,
        env_var=claude_code.MODEL_ENV_VAR,
        default=claude_code.MODEL_DEFAULT,
        note=claude_code.model.note,
    ),
}


def register_tool(name: str, tool: AITool) -> None:
    """Register a tool under a normalized name; used by tests and providers."""
    _TOOLS[name.strip().lower()] = tool


def unregister_tool(name: str) -> None:
    """Forget a registered tool; used by tests to restore the registry."""
    _TOOLS.pop(name.strip().lower(), None)


def all_tools() -> dict[str, AITool]:
    """Return a copy of the registered tools keyed by name."""
    return dict(_TOOLS)


def tools_for_role(role: str) -> list[str]:
    """Return registered tool names that serve ``role``, in display order."""
    return [
        tool.name
        for tool in sorted(
            (tool for tool in _TOOLS.values() if role in tool.roles),
            key=lambda tool: (tool.order, tool.name),
        )
    ]


def manageable_tools() -> list[str]:
    """Return the names of tools whose model this agent can switch."""
    return [tool.name for tool in _TOOLS.values() if tool.manageable]


def auth_tools() -> list[AITool]:
    """Return tools that declare an auth observation, in display order."""
    return sorted(
        (tool for tool in _TOOLS.values() if tool.auth is not None),
        key=lambda tool: (tool.order, tool.name),
    )


def limits_tools() -> list[AITool]:
    """Return tools that declare a limits snapshot key, in display order."""
    return sorted(
        (tool for tool in _TOOLS.values() if tool.limits_key is not None),
        key=lambda tool: (tool.order, tool.name),
    )


def token_tools() -> list[str]:
    """Return usage-capable tools, including those with unknown token counts."""
    return [
        tool.name
        for tool in sorted(
            (tool for tool in _TOOLS.values() if tool.roles),
            key=lambda tool: (tool.order, tool.name),
        )
    ]


def known_tools() -> list[str]:
    """Return the registered tool names in display order."""
    return list(_TOOLS)


def get_tool(name: str) -> AITool | None:
    """Resolve a case-insensitive tool name, or return None if unknown."""
    return _TOOLS.get(name.strip().lower())


def all_info() -> list[ModelInfo]:
    """Describe the current model for every registered tool."""
    return [tool.info() for tool in _TOOLS.values()]
