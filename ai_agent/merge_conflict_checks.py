"""Application-observed checks required before publishing a merge resolution."""

import json
import subprocess
from pathlib import Path

from ai_agent.config import WORKER_ISOLATION_ENABLED
from ai_agent.provider_process import grouped_run
from ai_agent.verification import commands_for
from ai_agent.verification_environment import (
    command_environment,
    python_environment,
    verification_argv,
)


def check_merge(checkout: Path, project: str) -> None:
    """Run fixed repository checks; unknown checks or any failure block the push."""
    try:
        commands = commands_for(f"{project}:baseline-v1")
    except ValueError:
        package = checkout / "package.json"
        if package.is_file() and json.loads(package.read_text()).get("scripts", {}).get(
            "test"
        ):
            commands = (("npm", "test"),)
        elif (checkout / "pytest.ini").exists() or (
            checkout / "pyproject.toml"
        ).exists():
            commands = (("python", "-m", "pytest", "-q"),)
        else:
            raise RuntimeError(
                "No supported verification profile for this project."
            ) from None

    def checked(argv: list[str], python: Path | None = None) -> None:
        result = grouped_run(
            argv,
            cwd=checkout,
            env=command_environment(python),
            isolated=WORKER_ISOLATION_ENABLED,
            timeout=1800,
            stdin=subprocess.DEVNULL,
            text=True,
            max_output_chars=8192,
        )
        if result.returncode:
            raise RuntimeError(
                f"Merge verification failed: {' '.join(argv)}\n"
                f"{result.stdout or result.stderr or 'No output.'}"
            )

    if any(command[0] == "npm" for command in commands):
        checked(["npm", "ci", "--ignore-scripts"])
    with python_environment(checkout, commands) as python:
        for command in commands:
            checked(verification_argv(command, python), python)
