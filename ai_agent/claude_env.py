"""Run Claude Code on the owner's claude.ai login, not on the API key.

Claude Code prefers ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) over the
claude.ai login whenever either is in its environment. The agent's service
has the key for the Claude API limits card (and CLAUDE_PLANNER_VIA=api), so
every ``claude`` run inherited it: work was billed to the API instead of the
subscription, and the subscription's rate-limit windows never came back, so
the Claude Code limits went stale. Each ``claude`` process now gets the
environment without those variables. CLAUDE_CODE_AUTH=api_key keeps them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from ai_agent.config import CLAUDE_CODE_AUTH

API_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def claude_code_env(base: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The environment for a claude process; None = inherit unchanged."""
    if CLAUDE_CODE_AUTH == "api_key":
        return None
    env = dict(os.environ if base is None else base)
    for name in API_VARIABLES:
        env.pop(name, None)
    return env


def env_for(args: Sequence[str]) -> dict[str, str] | None:
    """claude_code_env() for a ``claude`` command, None for anything else."""
    if args and Path(args[0]).name == "claude":
        return claude_code_env()
    return None
