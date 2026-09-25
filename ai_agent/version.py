"""Report the coding agent version through the shared core."""

from pathlib import Path

from ai_agent_common import get_runtime_version as _shared_runtime_version

ROOT_DIR = Path(__file__).resolve().parent.parent


def get_runtime_version() -> str:
    # Delegates to ai-agent-common so all bots report version identically.
    """Return the shared-core version report for this agent."""
    return _shared_runtime_version("ai-coding-agent", ROOT_DIR)
