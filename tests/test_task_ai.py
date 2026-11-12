"""Task model history must survive settings changes without inventing evidence."""

from types import SimpleNamespace
from unittest.mock import patch

from ai_agent import config, task_ai
from ai_agent.context_attempts import current_attempt


def test_deepseek_bridge_and_explicit_cli_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(config, "CLAUDE_CODE_ARGS", ())
    assert task_ai.capture("claude", "executor") == {
        "tool": "claude-code",
        "model": "deepseek-v4-pro",
    }
    monkeypatch.setattr(config, "CLAUDE_CODE_ARGS", ("--model", "sonnet"))
    assert task_ai.capture("claude", "executor")["model"] == "sonnet"


def test_unknown_history_never_uses_current_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-v4-pro")
    assert task_ai.task_ai({}) == {"planner": None, "split": None, "implementer": None}
    old = {"run": {"implementation_agent": "claude"}}
    assert task_ai.task_ai(old)["implementer"] == {"tool": "claude"}
    assert task_ai.public_selection(
        {"tool": "codex", "model": "https://secret/key"}
    ) == {"tool": "codex"}


def test_recorded_model_wins_over_later_settings_and_matches_exact_task():
    task = {"id": "task", "repo": "repo", "branch": "feature/a"}
    other = {"id": "task", "repo": "other", "branch": "feature/a"}
    attempt = SimpleNamespace(
        request=SimpleNamespace(task_id="task"),
        state={
            "branch": "feature/a",
            "spec": {"scope": {"repository": {"project_name": "repo"}}},
        },
        data={"tasks": [task, other]},
    )
    token = current_attempt.set(attempt)
    try:
        with patch.object(task_ai, "capture", return_value={"tool": "claude-code"}):
            task_ai.record_execution("claude", "deepseek-v4-pro")
        assert task_ai.task_ai(task)["implementer"]["model"] == "deepseek-v4-pro"
        assert "implementation_ai" not in other
    finally:
        current_attempt.reset(token)
