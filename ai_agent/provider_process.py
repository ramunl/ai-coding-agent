"""Bound provider subprocess groups; refuse recovery while a writer may remain."""

import io
import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ai_agent.config import WORKER_GROUP, WORKER_USER
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


def _runner_name(args: list[str]) -> str:
    """The executable basename, used to select the denial-event contract."""
    return Path(args[0]).name if args else ""


def _decode(value: object) -> str:
    """Return bytes or str as decoded text, never raising."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def _write_stdin(process: subprocess.Popen[Any], input_text: Any) -> None:
    """Write and close the child's stdin, handling text and binary pipes."""
    if process.stdin is None or process.stdin.closed:
        return
    try:
        if input_text is not None:
            if isinstance(process.stdin, io.TextIOBase):
                data = (
                    input_text.decode(errors="replace")
                    if isinstance(input_text, bytes)
                    else input_text
                )
                process.stdin.write(data)
            else:
                data = (
                    input_text.encode() if isinstance(input_text, str) else input_text
                )
                process.stdin.write(data)
        process.stdin.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass


def _collect_streaming(
    process: subprocess.Popen[Any],
    args: list[str],
    timeout: int | None,
    stop_run: Any,
    input_text: Any,
    detector: Any,
    runner: str,
    attempt_id: str | None,
) -> tuple[str, str]:
    """Stream the child while honoring stop/timeout and the denial detector.

    stdout is read line by line so denial events are seen incrementally; stderr
    is drained on its own thread so a talkative child cannot deadlock. Raising
    here (stop, timeout or denial-loop blocker) hands control to the caller's
    kill-and-recover path.
    """
    from ai_agent.implementation_stop import ImplementationStoppedError

    stderr_lines: list[str] = []
    line_queue: "queue.Queue[str | None]" = queue.Queue()

    def drain_stderr() -> None:
        try:
            while True:
                line = process.stderr.readline()
                if not line:
                    break
                stderr_lines.append(_decode(line))
        except (OSError, ValueError):
            pass

    def read_stdout() -> None:
        try:
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                line_queue.put(_decode(line))
        except (OSError, ValueError):
            pass
        finally:
            line_queue.put(None)

    if process.stdin is not None and not process.stdin.closed:
        threading.Thread(
            target=_write_stdin, args=(process, input_text), daemon=True
        ).start()
    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    threading.Thread(target=read_stdout, daemon=True).start()

    stdout_lines: list[str] = []
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if stop_run is not None and stop_run.stop.is_set():
            raise ImplementationStoppedError(
                "Implementation stopped by owner; saved work is preserved."
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(args, timeout)
        try:
            line = line_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        if line is None:
            break
        stdout_lines.append(line)
        if detector is not None:
            blocker = detector.feed(line, runner=runner, attempt_id=attempt_id)
            if blocker is not None:
                if stop_run is not None:
                    stop_run.stop.set()
                raise blocker

    while True:
        if stop_run is not None and stop_run.stop.is_set():
            raise ImplementationStoppedError(
                "Implementation stopped by owner; saved work is preserved."
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(args, timeout)
        if process.poll() is not None and not stderr_thread.is_alive():
            break
        time.sleep(0.02)

    process.wait()
    return "".join(stdout_lines), "".join(stderr_lines)


def grouped_run(
    args: list[str], *, isolated: bool = False, **kwargs: Any
) -> subprocess.CompletedProcess:
    """A timeout kills the entire group before returning control to recovery.

    ``isolated`` is the implementer path: the child drops to the worker user
    (a no-op when the service already runs as that user) and inherits only the
    memfd image descriptors it actually references. Task-bound runs stream the
    child's stdout through the denial-loop detector so a repeated identical
    permission denial stops the paid run at the configured threshold.
    """
    repo = kwargs.get("cwd")
    marker = _marker(repo)
    require_no_writer(repo)
    timeout = kwargs.pop("timeout")
    kwargs.pop("capture_output", None)
    kwargs.pop("check", None)
    input_text = kwargs.pop("input", None)
    if input_text is not None:
        kwargs["stdin"] = subprocess.PIPE
    if isolated:
        kwargs["user"] = WORKER_USER
        kwargs["group"] = WORKER_GROUP
    from ai_agent import denial_loop
    from ai_agent.git_recovery import _session
    from ai_agent.image_delivery import open_fds
    from ai_agent.implementation_stop import register, unregister
    from ai_agent.usage_details import current_attempt_id

    session = _session.get()
    stop_run = register(session.task_id, session.branch) if session else None
    runner = _runner_name(args)
    # A crash between launch and recording the PID must fail closed too.
    try:
        marker.write_text(json.dumps({"group": None}))
    except BaseException:
        unregister(stop_run)
        raise
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=open_fds(),
            **kwargs,
        )
    except BaseException:
        unregister(stop_run)
        marker.unlink(missing_ok=True)
        raise
    streaming = stop_run is not None
    try:
        marker.write_text(json.dumps({"group": process.pid}))
        if streaming:
            detector = denial_loop.detector_for(runner, args)
            stdout, stderr = _collect_streaming(
                process,
                args,
                timeout,
                stop_run,
                input_text,
                detector,
                runner,
                current_attempt_id(),
            )
        else:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)

    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if streaming:
            # The stream reader threads already drain the pipes to EOF.
            pass
        else:
            process.communicate(timeout=10)
        _wait_stopped(process.pid)
        require_no_writer(repo)
        raise
    finally:
        unregister(stop_run)
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
