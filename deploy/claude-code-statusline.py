#!/usr/bin/env python3
"""Capture quota input and preserve an existing Claude Code status-line command."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_agent.claude_code_limits import capture_limits  # noqa: E402
from ai_agent.config import CLAUDE_CODE_LIMITS_FILE  # noqa: E402


def main() -> None:
    """Capture quota fields and forward the existing status-line output."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--forward-command")
    args = parser.parse_args()
    payload = sys.stdin.read()
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            capture_limits(data, CLAUDE_CODE_LIMITS_FILE)
    except (ValueError, TypeError) as error:
        print(
            f"Claude Code limit capture failed ({type(error).__name__})",
            file=sys.stderr,
        )
    if args.forward_command:
        try:
            # The forwarded command comes from the user's trusted existing settings.
            result = subprocess.run(
                args.forward_command,
                shell=True,
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
            print(result.stdout, end="")
            if result.returncode:
                print("Existing status-line command failed", file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired):
            print("Existing status-line command failed or timed out", file=sys.stderr)
    else:
        print("Claude Code · quota capture enabled")


if __name__ == "__main__":
    main()
