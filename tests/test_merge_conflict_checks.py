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
