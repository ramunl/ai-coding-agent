import os
import subprocess
import sys

from ai_agent.config import COMMAND_TIMEOUT_SECONDS


def run_unit_tests() -> str:
    env = os.environ.copy()
    env.update(
        {
            "TELEGRAM_BOT_TOKEN": "test-telegram-token",
            "YOUR_CHAT_ID": "1",
            "ANTHROPIC_API_KEY": "test-anthropic-key",
            "GITHUB_TOKEN": "test-github-token",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    output = result.stdout + result.stderr
    status = "passed" if result.returncode == 0 else "failed"
    return f"Tests {status} ({result.returncode}):\n{output}"
