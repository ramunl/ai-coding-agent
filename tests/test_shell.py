"""Regression coverage for captured output from timed-out commands."""

import importlib
import subprocess

import pytest


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        (b"out\n", None, "out\n"),
        (None, b"err\n", "err\n"),
        (None, None, ""),
        (b"", b"", ""),
        ("", "", ""),
        (b"out\xff", None, "out\ufffd"),
        (None, b"err\xfe", "err\ufffd"),
        ("out\n", None, "out\n"),
        (None, "err\n", "err\n"),
        ("out\n", "err\n", "out\nerr\n"),
        (b"out\n", b"err\n", "out\nerr\n"),
        (b"out\xff", "err\n", "out\ufffderr\n"),
        ("out\n", b"err\xfe", "out\nerr\ufffd"),
    ],
)
def test_timeout_normalizes_each_stream(
    monkeypatch, tmp_path, stdout, stderr, expected
):
    shell = importlib.import_module("ai_agent.shell")
    args = ["example", "argument"]
    timeout_error = subprocess.TimeoutExpired(args, 7, output=stdout, stderr=stderr)

    def timed_out(*args, **kwargs):
        raise timeout_error

    monkeypatch.setattr(shell.subprocess, "run", timed_out)

    with pytest.raises(RuntimeError) as caught:
        shell.run(args, cwd=tmp_path, timeout=7)

    assert (
        str(caught.value) == f"Command timed out after 7s: example argument\n{expected}"
    )
    assert caught.value.__cause__ is timeout_error


@pytest.mark.parametrize("output", [b"secret\xff", "secret", None])
def test_private_timeout_hides_command_and_output(
    monkeypatch, tmp_path, caplog, output
):
    shell = importlib.import_module("ai_agent.shell")
    args = ["example", "secret-argument"]

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args, 7, output=output, stderr=output)

    monkeypatch.setattr(shell.subprocess, "run", timed_out)

    with caplog.at_level("INFO"), pytest.raises(RuntimeError) as caught:
        shell.run(args, cwd=tmp_path, timeout=7, private=True)

    assert str(caught.value) == "Image provider command timed out."
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert "secret" not in caplog.text


def test_a_failure_shows_the_tool_error_not_the_whole_prompt(monkeypatch, tmp_path):
    shell = importlib.import_module("ai_agent.shell")
    prompt = "Implement the following change in this repository.\n" + "plan\n" * 200
    failed = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="ERROR: You've hit your usage limit.\n"
    )
    monkeypatch.setattr("ai_agent.provider_process.grouped_run", lambda *a, **k: failed)
    with pytest.raises(RuntimeError) as caught:
        shell.run(["codex", "exec", "-s", "workspace-write", prompt], cwd=tmp_path)
    assert str(caught.value) == (
        "Command failed (1): codex exec -s workspace-write "
        "Implement the following change in this repository.…\n"
        "ERROR: You've hit your usage limit.\n"
    )


def test_explicit_provider_uses_supervised_runner(monkeypatch, tmp_path):
    from ai_agent import provider_process, shell

    calls = []

    def supervised(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="done", stderr="")

    def unsupervised(*args, **kwargs):
        pytest.fail("Provider bypassed process supervision")

    monkeypatch.setattr(provider_process, "grouped_run", supervised)
    monkeypatch.setattr(shell.subprocess, "run", unsupervised)
    # The executable deliberately differs from the registered provider ID.
    result = shell.run(["/custom/provider-cli", "run"], cwd=tmp_path, provider_run=True)
    assert result.output == "done"
    assert calls == [["/custom/provider-cli", "run"]]


def test_routine_commands_are_debug_only_and_keep_diagnostic_options(
    monkeypatch, tmp_path, caplog
):
    shell = importlib.import_module("ai_agent.shell")
    monkeypatch.setattr(
        shell.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    with caplog.at_level("INFO", logger="ai_agent.shell"):
        shell.run(["example", "status"], cwd=tmp_path, timeout=17)
    assert "Running command" not in caplog.text
    caplog.clear()
    with caplog.at_level("DEBUG", logger="ai_agent.shell"):
        shell.run(["example", "status"], cwd=tmp_path, timeout=17)
        shell.run(["example", "private-value"], cwd=tmp_path, private=True)
    assert "timeout=17 interactive=False" in caplog.text
    assert "private-value" not in caplog.text
    assert "[private input]" in caplog.text
