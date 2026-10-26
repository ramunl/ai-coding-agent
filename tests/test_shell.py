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
