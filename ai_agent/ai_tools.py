"""A thin /model shim over the per-provider modules.

The per-tool contract (roles, adapters, model selection, auth, limits, error
classification, images, structured output, usage, enabled) lives in
``ai_agent/providers`` and is discovered by ``ai_agent.provider_registry``.
This module keeps the small AITool surface the /model command, dashboard
snapshot and tests already use, delegating each method to the same
implementation modules as before so saved selections and patches keep working.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ai_agent import codex_models, model_manager
from ai_agent.providers import claude, claude_code, codex


@dataclass(frozen=True)
class ModelInfo:
    """Describe a tool model and whether the agent can change it."""

    tool: str
    model: str
    manageable: bool  # can this agent change the model?
    note: str = ""  # where the model actually lives, when not manageable


class AITool:
    """Base interface. Subclasses fill in what is real for that tool."""

    name: str
    manageable: bool = False

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


class ClaudeApiTool(AITool):
    """The Anthropic API planner: model is a string we own and can verify."""

    name = claude.name
    manageable = claude.model.manageable

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


class CodexTool(AITool):
    """Select an agent override using the installed CLI's model catalog."""

    name = codex.name
    manageable = codex.model.manageable

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


_TOOLS: dict[str, AITool] = {
    "claude": ClaudeApiTool(),
    "codex": CodexTool(),
    "claude-code": CliTool(
        claude_code.name,
        env_var=claude_code.MODEL_ENV_VAR,
        default=claude_code.MODEL_DEFAULT,
        note=claude_code.model.note,
    ),
}


def known_tools() -> list[str]:
    """Return the registered tool names in display order."""
    return list(_TOOLS)


def get_tool(name: str) -> AITool | None:
    """Resolve a case-insensitive tool name, or return None if unknown."""
    return _TOOLS.get(name.strip().lower())


def all_info() -> list[ModelInfo]:
    """Describe the current model for every registered tool."""
    return [tool.info() for tool in _TOOLS.values()]
