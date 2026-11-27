"""Verify application checks cannot report success after a failed command."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from ai_agent import merge_conflict_checks as checks


def test_failed_command_blocks_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(
        checks, "python_environment", lambda *_: nullcontext(tmp_path / "python")
    )
    monkeypatch.setattr(
        checks,
        "grouped_run",
        lambda *_, **__: SimpleNamespace(
            returncode=1, stdout="test failure", stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="test failure"):
        checks.check_merge(tmp_path, "ai-coding-agent")


def test_unknown_project_without_checks_is_blocked(tmp_path):
    with pytest.raises(RuntimeError, match="No supported verification"):
        checks.check_merge(tmp_path, "unknown")


def test_pytest_failure_summary_survives_dashboard_limit(tmp_path, monkeypatch):
    python = tmp_path / "verification-env" / "bin" / "python"
    monkeypatch.setattr(checks, "python_environment", lambda *_: nullcontext(python))

    def run(argv, **kwargs):
        if "pytest" not in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        output = "." * 2000 + "\n" + "traceback\n" * 250
        output += "FAILED tests/test_models.py::test_default_model - AssertionError\n"
        return SimpleNamespace(returncode=1, stdout=output, stderr="")

    monkeypatch.setattr(checks, "grouped_run", run)
    with pytest.raises(RuntimeError) as error:
        checks.check_merge(tmp_path, "ai-coding-agent")
    message = str(error.value)[:2000]
    assert "python -m pytest -q" in message
    assert "tests/test_models.py::test_default_model" in message
    assert str(python) not in message
