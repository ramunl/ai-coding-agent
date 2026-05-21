import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai_agent.config import COMMAND_TIMEOUT_SECONDS, REPO_PATH


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    output: str


def run(args: list[str], cwd: Path = REPO_PATH, timeout: int = COMMAND_TIMEOUT_SECONDS) -> CommandResult:
    logger.info("Running command: %s cwd=%s timeout=%s", args, cwd, timeout)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            cwd=cwd,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(args)}\n{output}") from exc

    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}\n{output}")
    return CommandResult(args=args, returncode=result.returncode, output=output)
