"""Provider descendants must stop before a checkpoint can touch the checkout."""

import json
import os
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from ai_agent import provider_process


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(
        provider_process, "active_project", lambda: SimpleNamespace(repo_path=tmp_path)
    )
    return tmp_path


def test_timeout_kills_descendant_writer_before_recovery(repo):
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c', "
        "\"import time,pathlib; time.sleep(1); pathlib.Path('late').write_text('bad')\"]); "
        "time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        provider_process.grouped_run(
            [sys.executable, "-c", script], cwd=repo, timeout=0.15, text=True
        )
    provider_process.require_no_writer()
    time.sleep(1.1)
    assert not (repo / "late").exists()


def test_success_kills_detached_stdio_descendant(repo):
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c', "
        "\"import time,pathlib; time.sleep(1); pathlib.Path('late').write_text('bad')\"],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    )
    result = provider_process.grouped_run(
        [sys.executable, "-c", script], cwd=repo, timeout=5, text=True
    )
    assert result.returncode == 0
    provider_process.require_no_writer()
    time.sleep(1.1)
    assert not (repo / "late").exists()


@pytest.mark.parametrize("value", [[], {"group": None}, {"group": "1"}])
def test_unknown_process_marker_fails_closed(repo, value):
    provider_process._marker().write_text(json.dumps(value))
    with pytest.raises(RuntimeError):
        provider_process.require_no_writer()


def test_explicit_repository_marker_is_used(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", str(other)], check=True, capture_output=True)
    assert provider_process._marker(other) == other / ".git/ai-agent-provider.json"


def test_grouped_provider_can_read_private_memory_attachment(repo):
    import os

    from ai_agent.image_delivery import codex_files

    if not hasattr(os, "memfd_create"):
        pytest.skip("Python interpreter does not expose Linux memfd_create")
    with codex_files([("image/png", b"private image bytes")]) as flags:
        result = provider_process.grouped_run(
            [
                sys.executable,
                "-c",
                "import sys; print(len(open(sys.argv[1], 'rb').read()))",
                flags[1],
            ],
            cwd=repo,
            timeout=5,
            text=True,
        )
    assert result.stdout.strip() == "19"
    provider_process.require_no_writer()


@pytest.mark.parametrize("via_shell", [False, True])
def test_owner_stop_kills_descendants_and_preserves_edits(repo, via_shell):
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from ai_agent.git_recovery import RecoverySession, _session
    from ai_agent.implementation_stop import ImplementationStoppedError, request, status

    token = _session.set(RecoverySession(task_id="task1", branch="feature/test"))
    script = (
        "import subprocess,sys,time,pathlib; pathlib.Path('saved').write_text('keep'); "
        "subprocess.Popen([sys.executable,'-c',"
        "\"import time,pathlib; time.sleep(1.5); pathlib.Path('late').write_text('bad')\"]); "
        "time.sleep(30)"
    )
    from ai_agent import shell

    runner = shell.run if via_shell else provider_process.grouped_run
    options = {"provider_run": True} if via_shell else {"text": True}
    context = contextvars.copy_context()
    try:
        with ThreadPoolExecutor() as pool:
            future = pool.submit(
                context.run,
                runner,
                [sys.executable, "-c", script],
                cwd=repo,
                timeout=None,
                **options,
            )
            deadline = time.monotonic() + 5
            while not (repo / "saved").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            info = status("task1", "feature/test")
            assert info["can_stop"]
            with pytest.raises(ValueError):
                request("task1", "feature/test", "stale")
            request("task1", "feature/test", info["stop_token"])
            request("task1", "feature/test", info["stop_token"])
            with pytest.raises(ImplementationStoppedError):
                future.result(timeout=5)
        provider_process.require_no_writer(repo)
        assert (repo / "saved").read_text() == "keep"
        time.sleep(1.6)
        assert not (repo / "late").exists()
        assert not status("task1", "feature/test")["can_stop"]
    finally:
        _session.reset(token)


def test_stale_stop_capability_cannot_stop_next_attempt():
    from ai_agent.implementation_stop import register, request, unregister

    first = register("task1", "branch")
    unregister(first)
    second = register("task1", "branch")
    try:
        with pytest.raises(ValueError):
            request("task1", "branch", first.token)
        assert not second.stop.is_set()
    finally:
        unregister(second)


@pytest.fixture
def claude_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


def _write_fake_claude(bindir, body):
    script = bindir / "claude"
    script.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}\n")
    script.chmod(0o755)
    return script


def test_denial_loop_blocks_kills_group_and_preserves_edits(
    repo, claude_on_path, monkeypatch
):
    from ai_agent import denial_loop
    from ai_agent.denial_loop import DenialLoopBlockedError
    from ai_agent.git_recovery import RecoverySession, _session

    body = """
        import json, subprocess, sys, time, pathlib
        pathlib.Path('saved').write_text('keep')
        line = json.dumps({"type": "tool_use_result", "tool_name": "Bash",
            "denial": {"denied": True, "tool": "Bash", "operation": "git push",
            "policy": "disallowed_tool"}})
        for _ in range(3):
            print(line, flush=True)
        subprocess.Popen([sys.executable, '-c',
            "import time,pathlib; time.sleep(1.2); pathlib.Path('late').write_text('bad')"])
        time.sleep(30)
    """
    _write_fake_claude(claude_on_path, body)
    monkeypatch.setattr(denial_loop, "probe_version", lambda runner: "2.1.318")

    token = _session.set(RecoverySession(task_id="task1", branch="feature/test"))
    try:
        with pytest.raises(DenialLoopBlockedError):
            provider_process.grouped_run(
                ["claude", "-p", "--output-format", "stream-json", "--verbose"],
                cwd=repo,
                timeout=10,
                text=True,
            )
        provider_process.require_no_writer(repo)
        assert (repo / "saved").read_text() == "keep"
        time.sleep(1.3)
        assert not (repo / "late").exists()
    finally:
        _session.reset(token)


def test_denial_loop_ignores_ordinary_tool_errors(repo, claude_on_path, monkeypatch):
    from ai_agent import denial_loop
    from ai_agent.git_recovery import RecoverySession, _session

    body = """
        import time
        print('{"type": "result", "is_error": true, "result": "tests failed"}',
            flush=True)
        time.sleep(0.05)
    """
    _write_fake_claude(claude_on_path, body)
    monkeypatch.setattr(denial_loop, "probe_version", lambda runner: "2.1.318")

    token = _session.set(RecoverySession(task_id="task1", branch="feature/test"))
    try:
        result = provider_process.grouped_run(
            ["claude", "-p", "--output-format", "stream-json", "--verbose"],
            cwd=repo,
            timeout=10,
            text=True,
        )
        assert result.returncode == 0
        provider_process.require_no_writer(repo)
    finally:
        _session.reset(token)


def test_denial_loop_unique_denials_do_not_block(repo, claude_on_path, monkeypatch):
    from ai_agent import denial_loop
    from ai_agent.git_recovery import RecoverySession, _session

    body = """
        import time
        print('{"type": "tool_use_result", "tool_name": "Bash", "denial": '
              '{"denied": true, "tool": "Bash", "operation": "op-a", "policy": "denied"}}',
              flush=True)
        print('{"type": "tool_use_result", "tool_name": "Bash", "denial": '
              '{"denied": true, "tool": "Bash", "operation": "op-b", "policy": "denied"}}',
              flush=True)
        time.sleep(0.05)
    """
    _write_fake_claude(claude_on_path, body)
    monkeypatch.setattr(denial_loop, "probe_version", lambda runner: "2.1.318")

    token = _session.set(RecoverySession(task_id="task1", branch="feature/test"))
    try:
        result = provider_process.grouped_run(
            ["claude", "-p", "--output-format", "stream-json", "--verbose"],
            cwd=repo,
            timeout=10,
            text=True,
        )
        assert result.returncode == 0
        provider_process.require_no_writer(repo)
    finally:
        _session.reset(token)
