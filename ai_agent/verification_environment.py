"""Disposable repository development dependencies for fixed verification checks."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_agent.claude_env import API_VARIABLES, provider_env
from ai_agent.config import WORKER_GROUP, WORKER_ISOLATION_ENABLED, WORKER_USER
from ai_agent.context_attempts import current_attempt
from ai_agent.run_outcomes import NoCodeOutcomeError, blocked_report


def command_environment(python: Path | None = None) -> dict[str, str]:
    """Test/package commands receive no model credentials or external Python paths."""
    env = provider_env(worker=WORKER_ISOLATION_ENABLED)
    for name in (
        *API_VARIABLES,
        "PYTHONPATH",
        "PYTHONHOME",
        "PIP_TARGET",
        "PIP_PREFIX",
        "PIP_USER",
    ):
        env.pop(name, None)
    env["PYTHONNOUSERSITE"] = "1"
    if python is not None:
        env["PIP_REQUIRE_VIRTUALENV"] = "true"
        env["VIRTUAL_ENV"] = str(python.parent.parent)
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    return env


def _worker_owns(path: Path) -> None:
    """A worker must be able to build its environment outside the Git worktree."""
    if WORKER_ISOLATION_ENABLED and os.geteuid() == 0:
        import grp
        import pwd

        os.chown(
            path, pwd.getpwnam(WORKER_USER).pw_uid, grp.getgrnam(WORKER_GROUP).gr_gid
        )


def _prepare(
    args: list[str], worktree: Path, step: str, python: Path | None = None
) -> None:
    from ai_agent.provider_process import grouped_run

    try:
        result = grouped_run(
            args,
            cwd=worktree,
            env=command_environment(python),
            isolated=WORKER_ISOLATION_ENABLED,
            timeout=600,
            stdin=subprocess.DEVNULL,
            text=True,
            max_output_chars=8192,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NoCodeOutcomeError(
            blocked_report(
                f"Verification environment setup failed: {step} "
                f"({type(error).__name__}).",
                next_step="Check venv, pip and package access, then retry.",
            )
        ) from error
    attempt = current_attempt.get()
    if attempt:
        attempt.checkpoint(
            "verification_setup",
            {"step": step, "exit_code": result.returncode},
            required=True,
        )
    if result.returncode != 0:
        raise NoCodeOutcomeError(
            blocked_report(
                f"Verification environment setup failed: {step} "
                f"(exit {result.returncode}).",
                evidence=result.stdout or result.stderr or "No output.",
                next_step="Resolve the development dependency setup error, then retry.",
            )
        )


@contextmanager
def python_environment(
    worktree: Path, commands: tuple[tuple[str, ...], ...]
) -> Iterator[Path | None]:
    """Install this commit's declared development requirements in a temporary venv.

    Python and Ruff checks use that environment, never the controller's packages.
    The directory is outside the checkout and removed on success, failure or stop.
    JavaScript-only profiles do not create a Python environment.
    """
    if not any(command[0] in {"python", "ruff"} for command in commands):
        yield None
        return
    requirements = worktree / "requirements-dev.txt"
    if not requirements.is_file():
        raise NoCodeOutcomeError(
            blocked_report(
                "Cannot prepare verification: requirements-dev.txt is missing.",
                next_step="Restore development requirements, then retry.",
            )
        )
    with TemporaryDirectory(
        prefix="verification-env-", dir=worktree.parent
    ) as directory:
        root = Path(directory)
        _worker_owns(root)
        _prepare(
            [sys.executable, "-m", "venv", str(root)],
            worktree,
            "create isolated Python environment",
        )
        python = root / "bin" / "python"
        _prepare(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-r",
                "requirements-dev.txt",
            ],
            worktree,
            "install repository development requirements",
            python,
        )
        _prepare(
            [str(python), "-m", "pip", "check"],
            worktree,
            "check development dependency consistency",
            python,
        )
        yield python


def verification_argv(command: tuple[str, ...], python: Path | None) -> list[str]:
    """Bind Python and Ruff to the prepared environment with no PATH fallback."""
    if command[0] not in {"python", "ruff"}:
        return list(command)
    if python is None:
        raise RuntimeError("Python verification environment was not prepared")
    prefix = [str(python)] if command[0] == "python" else [str(python), "-m", "ruff"]
    return prefix + list(command[1:])
