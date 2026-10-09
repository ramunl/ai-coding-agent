"""A common interface for setting the model of each AI tool.

Scope (deliberately limited): this manages the *model dial* per tool, not
auth, keys, or how each tool is invoked. Those stay tool-specific.

Claude API and Codex have agent-managed model overrides. Claude Code remains
read-only and uses its own CLI configuration.

Adding a future tool = one new AITool subclass registered below. The /model
command, registry, and tests do not change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ai_agent import codex_models, model_manager


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

    name = "claude"
    manageable = True

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


class CodexTool(AITool):
    """Select an agent override using the installed CLI's model catalog."""

    name = "codex"
    manageable = True

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
        return "Used for every Codex role. Changes apply to the next run."


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
        "claude-code",
        env_var="CLAUDE_CODE_MODEL",
        default="(claude CLI default)",
        note="Managed by the Claude CLI's own login/config, not this agent.",
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
