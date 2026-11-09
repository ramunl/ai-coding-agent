"""Run provider CLIs without the service's secrets.

Claude Code prefers ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) over the
claude.ai login whenever either is in its environment. The agent's service
has the key for the Claude API limits card (and CLAUDE_PLANNER_VIA=api), so
every ``claude`` run inherited it: work was billed to the API instead of the
subscription, and the subscription's rate-limit windows never came back, so
the Claude Code limits went stale. Each ``claude`` process now gets the
environment without those variables. CLAUDE_CODE_AUTH=api_key keeps them.

DS12 extends the same stripping to every provider CLI (codex and claude): a
provider must never see the owner's GitHub token or SSH agent (it has no
business pushing or fetching as the owner), the DeepSeek key, or the Telegram
token. CLAUDE_CODE_AUTH stays in the environment so the worker's claude.ai
subscription login is preserved; ``worker=True`` points HOME at the worker's
own cache instead of /root.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from ai_agent.config import CLAUDE_CODE_AUTH, WORKER_HOME

API_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
PROVIDER_COMMANDS = ("codex", "claude", "opencode")
# Credentials a provider CLI must never see, whatever its role.
PROVIDER_SECRETS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "DEEPSEEK_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
)


def claude_code_env(base: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The environment for a claude process; None = inherit unchanged."""
    if CLAUDE_CODE_AUTH == "api_key":
        return None
    env = dict(os.environ if base is None else base)
    for name in API_VARIABLES:
        env.pop(name, None)
    return env


def provider_env(
    base: Mapping[str, str] | None = None, *, worker: bool = False
) -> dict[str, str]:
    """The environment for any provider CLI: secrets removed, worker HOME set.

    ``worker=True`` is the isolated implementer environment: HOME points at the
    worker-owned cache so codex/claude find their login there, never /root.
    """
    env = dict(os.environ if base is None else base)
    if CLAUDE_CODE_AUTH != "api_key":
        for name in API_VARIABLES:
            env.pop(name, None)
    for name in PROVIDER_SECRETS:
        env.pop(name, None)
    env["CLAUDE_CODE_AUTH"] = CLAUDE_CODE_AUTH
    if worker:
        env["HOME"] = str(WORKER_HOME)
    return env


def env_for(args: Sequence[str]) -> dict[str, str] | None:
    """provider_env() for a provider CLI, None for any other command."""
    if args and Path(args[0]).name in PROVIDER_COMMANDS:
        return provider_env()
    return None
