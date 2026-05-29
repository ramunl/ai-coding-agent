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


def run(args: list[str], cwd: Path = REPO_PATH, timeout: int = COMMAND_TIMEOUT_SECONDS, interactive: bool = False) -> CommandResult:
    logger.info("Running command: %s cwd=%s timeout=%s interactive=%s", args, cwd, timeout, interactive)
    try:
        result = subprocess.run(
            args,
            capture_output=not interactive,
            stdin=None if interactive else subprocess.DEVNULL,
            check=False,
            cwd=cwd,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(args)}\n{output}") from exc

    output = (result.stdout or "") + (result.stderr or "") if not interactive else ""
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(args)}\n{output}")
    return CommandResult(args=args, returncode=result.returncode, output=output)
