"""Install passive quota capture without replacing existing status-line output."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from ai_agent.bot.atomic_file import write_json_atomic


def install_statusline(settings_path: Path, script: Path) -> bool:
    """Wrap the current command once, preserving all other Claude settings."""
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError("Claude settings must be a JSON object")
    previous = settings.get("statusLine", {})
    if not isinstance(previous, dict):
        raise ValueError("Existing status line must be an object")
    command = previous.get("command", "")
    if not isinstance(command, str):
        raise ValueError("Existing status-line command must be text")
    if str(script) in shlex.split(command):
        return False
    backup = settings_path.with_suffix(".json.before-quota-capture")
    if settings_path.exists() and not backup.exists():
        if not write_json_atomic(backup, settings):
            raise OSError("Could not back up Claude settings")
    arguments = [sys.executable, str(script)]
    if command:
        arguments.extend(["--forward-command", command])
    settings["statusLine"] = {
        **previous,
        "type": "command",
        "command": shlex.join(arguments),
    }
    if not write_json_atomic(settings_path, settings):
        raise OSError("Could not install Claude Code status line")
    return True


def main() -> None:
    """Install capture for this account's interactive Claude Code sessions."""
    script = Path(__file__).resolve().parents[1] / "deploy/claude-code-statusline.py"
    installed = install_statusline(Path.home() / ".claude/settings.json", script)
    print("Claude Code quota capture installed" if installed else "Already installed")


if __name__ == "__main__":
    main()
