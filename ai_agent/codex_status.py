from pathlib import Path

from ai_agent.config import COMMAND_TIMEOUT_SECONDS
from ai_agent.shell import run


def get_codex_status() -> str:
    version = run(["codex", "--version"], Path("/"), COMMAND_TIMEOUT_SECONDS).output.strip()
    login_status = run(["codex", "login", "status"], Path("/"), COMMAND_TIMEOUT_SECONDS).output.strip()

    return (
        "Codex status:\n"
        f"- CLI: {version or 'installed'}\n"
        f"- Login: {login_status or 'unknown'}\n"
        "- Plan limits remaining: not exposed by the Codex CLI/API\n\n"
        "Check remaining Codex plan usage in the Codex/OpenAI UI when a usage banner appears."
    )
