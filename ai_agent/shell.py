"""Run bounded shell commands in the active repository."""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai_agent.config import COMMAND_TIMEOUT_SECONDS
from ai_agent.image_delivery import private_failure
from ai_agent.projects import active_project

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """Capture a command, exit status, and combined output."""

    args: list[str]
    returncode: int
    output: str


ARGUMENT_LENGTH = 60


def command_text(args: list[str]) -> str:
    """The command for an error message, each long or multi-line argument cut.

    Implementers get the whole plan as one argument; printed in full it pushed
    the tool's own error (a usage limit, a login problem) out of the chat.
    """
    shown = []
    for argument in args:
        first = argument.split("\n", 1)[0]
        if first != argument or len(first) > ARGUMENT_LENGTH:
            first = first[:ARGUMENT_LENGTH].rstrip() + "…"
        shown.append(first)
    return " ".join(shown)


def run(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    interactive: bool = False,
    input_text: str | None = None,
    private: bool = False,
) -> CommandResult:
    # Resolved per call, not at import: the active project can change at runtime.
    """Run a command with a timeout and raise on failure."""
    working_directory = cwd if cwd is not None else active_project().repo_path
    logger.info(
        "Running command: %s cwd=%s timeout=%s interactive=%s",
        [args[0], "[private input]"] if private else args,
        working_directory,
        timeout,
        interactive,
    )
    try:
        result = subprocess.run(
            args,
            capture_output=not interactive,
            **(
                {"input": input_text}
                if input_text is not None
                else {"stdin": None if interactive else subprocess.DEVNULL}
            ),
            check=False,
            cwd=working_directory,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        if private:
            raise RuntimeError("Image provider command timed out.") from None
        output = "".join(
            stream.decode(errors="replace")
            if isinstance(stream, bytes)
            else stream or ""
            for stream in (exc.stdout, exc.stderr)
        )
        raise RuntimeError(
            f"Command timed out after {timeout}s: {command_text(args)}\n{output}"
        ) from exc

    output = (result.stdout or "") + (result.stderr or "") if not interactive else ""
    if result.returncode != 0:
        if private:
            raise RuntimeError(private_failure(output))
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command_text(args)}\n{output}"
        )
    if private:
        for argument in args:
            if argument.startswith("/proc/"):
                output = output.replace(argument, "[image attachment]")
    return CommandResult(
        args=[args[0]] if private else args, returncode=result.returncode, output=output
    )
