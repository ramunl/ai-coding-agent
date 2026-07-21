"""A common interface for setting the model of each AI tool.

Scope (deliberately limited): this manages the *model dial* per tool, not
auth, keys, or how each tool is invoked. Those stay tool-specific.

Reality the interface must represent honestly:
- The Claude API planner has a model string this agent controls and can
  verify against the live API. Fully manageable.
- The Codex CLI and Claude Code CLI choose their model from their own
  login/config. This agent does not control or verify them, so they are
  exposed read-only, pointing the user at the right place.

Adding a future tool = one new AITool subclass registered below. The /model
command, registry, and tests do not change.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_agent import model_manager


@dataclass(frozen=True)
class ModelInfo:
    tool: str
    model: str
    manageable: bool  # can this agent change the model?
    note: str = ""    # where the model actually lives, when not manageable


class AITool:
    """Base interface. Subclasses fill in what is real for that tool."""

    name: str
    manageable: bool = False

    def current_model(self) -> str:
        raise NotImplementedError

    def set_model(self, model: str) -> None:
        raise NotImplementedError

    def verify(self, model: str) -> tuple[bool, str]:
        raise NotImplementedError

    def info(self) -> ModelInfo:
        return ModelInfo(self.name, self.current_model(), self.manageable, self._note())

    def _note(self) -> str:
        return ""


class ClaudeApiTool(AITool):
    """The Anthropic API planner: model is a string we own and can verify."""

    name = "claude"
    manageable = True

    def current_model(self) -> str:
        return model_manager.active_model()

    def set_model(self, model: str) -> None:
        model_manager.set_model_in_env(model)

    def verify(self, model: str) -> tuple[bool, str]:
        return model_manager.verify_model(model)


class CliTool(AITool):
    """A CLI whose model lives in its own config; read-only from here."""

    manageable = False

    def __init__(self, name: str, env_var: str, default: str, note: str) -> None:
        self.name = name
        self._env_var = env_var
        self._default = default
        self._note_text = note

    def current_model(self) -> str:
        import os

        # Best-effort display only: some setups pin the CLI model via env.
        return os.environ.get(self._env_var, self._default)

    def _note(self) -> str:
        return self._note_text


_TOOLS: dict[str, AITool] = {
    "claude": ClaudeApiTool(),
    "codex": CliTool(
        "codex",
        env_var="CODEX_MODEL",
        default="(codex CLI default)",
        note="Managed by the Codex CLI's own login/config, not this agent.",
    ),
    "claude-code": CliTool(
        "claude-code",
        env_var="CLAUDE_CODE_MODEL",
        default="(claude CLI default)",
        note="Managed by the Claude CLI's own login/config, not this agent.",
    ),
}


def known_tools() -> list[str]:
    return list(_TOOLS)


def get_tool(name: str) -> AITool | None:
    return _TOOLS.get(name.strip().lower())


def all_info() -> list[ModelInfo]:
    return [tool.info() for tool in _TOOLS.values()]
