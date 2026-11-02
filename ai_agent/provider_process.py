"""Bound provider subprocess groups; refuse recovery while a writer may remain."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from ai_agent.projects import active_project


def _marker(repo: Path | None = None) -> Path:
    # Keep this outside the worktree; the Git run lease serializes writers.
    repo = repo or active_project().repo_path
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return (repo / result.stdout.strip()).resolve() / "ai-agent-provider.json"


def _alive(group: int) -> bool:
    # Linux zombies cannot write. Treat every non-zombie group member as live.
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(stat[2]) == group and stat[0] != "Z":
                    return True
            except (FileNotFoundError, ProcessLookupError):
                continue
        return False
    except OSError:
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False


def require_no_writer(repo: Path | None = None) -> None:
    """Fail closed on an unverified or still-live provider process group."""
    marker = _marker(repo)
    if not marker.exists():
        return
    try:
        value = json.loads(marker.read_text())
        if not isinstance(value, dict):
            raise ValueError("Invalid provider marker")
        group = value["group"]
        if not isinstance(group, int) or group <= 1 or _alive(group):
            raise RuntimeError(
                "Provider processes may still be running; recovery refused."
            )
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError(
            "Cannot verify provider shutdown; recovery refused."
        ) from None
    marker.unlink(missing_ok=True)


def grouped_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """A timeout kills the entire group before returning control to recovery."""
    repo = kwargs.get("cwd")
    marker = _marker(repo)
    require_no_writer(repo)
    timeout = kwargs.pop("timeout")
    kwargs.pop("capture_output", None)
    kwargs.pop("check", None)
    input_text = kwargs.pop("input", None)
    if input_text is not None:
        kwargs["stdin"] = subprocess.PIPE
    # A crash between launch and recording the PID must fail closed too.
    marker.write_text(json.dumps({"group": None}))
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            **kwargs,
        )
    except BaseException:
        marker.unlink(missing_ok=True)
        raise
    try:
        marker.write_text(json.dumps({"group": process.pid}))
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=10)
        _wait_stopped(process.pid)
        require_no_writer(repo)
        raise
    # A CLI can exit leaving descendants with stdout closed. Kill them too.
    if _alive(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _wait_stopped(process.pid)
    require_no_writer(repo)
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _wait_stopped(group: int) -> None:
    deadline = time.monotonic() + 5
    while _alive(group):
        if time.monotonic() >= deadline:
            raise RuntimeError("Provider descendants still live; recovery refused.")
        time.sleep(0.02)
