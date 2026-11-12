"""No-CI publication finishes its archive without claiming verification passed."""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_agent import ci
from ai_agent import context_attempts as journal
from ai_agent import context_lifecycle as lifecycle
from ai_agent import context_recovery as recovery
from ai_agent.context_store import ContextStore


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    factory = lambda scope, root=None: ContextStore(scope, root or tmp_path / "context")
    monkeypatch.setattr(lifecycle, "ContextStore", factory)
    monkeypatch.setattr(journal, "ContextStore", factory)
    monkeypatch.setattr(recovery, "ContextStore", factory)
    monkeypatch.setattr(recovery, "_recovery_lease", lambda _: nullcontext(tmp_path))
    spec = lifecycle.new_spec(
        "123",
        {
            "project_name": "repo",
            "repo_path": str(tmp_path),
            "github_repository": "owner/repo",
        },
        "11111111",
    )
    task = {"id": "11111111", "repo": "repo", "branch": "feature/a", "stage": "ended"}
    data = {"tasks": [task]}
    return spec, task, data


def test_restart_archives_no_ci_and_restores_pr_without_test_success(
    scenario, monkeypatch
):
    from ai_agent import github, shell

    spec, task, data = scenario
    attempt = journal.Attempt(spec, "feature/a", data)
    attempt.save()
    attempt.intent("pr")
    monkeypatch.setattr(shell, "run", lambda *a, **k: SimpleNamespace(output="a" * 40))
    monkeypatch.setattr(
        github,
        "github_request",
        lambda *a, **k: [
            {
                "number": 1,
                "state": "open",
                "html_url": "https://github.com/owner/repo/pull/1",
                "head": {
                    "sha": "a" * 40,
                    "ref": "feature/a",
                    "repo": {"full_name": "owner/repo"},
                },
            }
        ],
    )
    monkeypatch.setattr(
        ci,
        "evaluate_ci",
        lambda *a, **k: ci.CiResult("not_configured", "CI was not run"),
    )
    assert recovery.reconcile(data, 123)
    assert task["stage"] == "pr"
    assert task["pr_url"].endswith("/pull/1")
    assert task["context_handoff"]
    assert not data["context_recovery_pending"]
    record = attempt.store.get("attempt", attempt.state["attempt_id"])
    assert record.outcome == "succeeded"
    assert "no CI checks ran" in record.summary
    assert any("not_configured" in e.summary for e in record.evidence)
    assert not any("passed" in e.summary for e in record.evidence)


def test_monitor_returns_without_sleep_or_repair_when_ci_is_absent(monkeypatch):
    from tests.bot_fixtures import TelegramTestCase

    case = TelegramTestCase()
    case.setUp()
    try:
        from ai_agent.bot import ci_monitor

        monkeypatch.setattr(
            ci_monitor,
            "evaluate_ci",
            lambda *a, **k: ci.CiResult("not_configured", "CI was not run"),
        )
        sleep = AsyncMock(side_effect=AssertionError("Must not poll missing CI"))
        monkeypatch.setattr(ci_monitor.asyncio, "sleep", sleep)
        reply = AsyncMock()
        monkeypatch.setattr(ci_monitor, "reply_chunks", reply)
        result = asyncio.run(ci_monitor.watch_ci(None, "a" * 40))
        assert result.state == "not_configured"
        sleep.assert_not_awaited()
        assert "CI was not run" in str(reply.call_args_list)
    finally:
        case.tearDown()
        case.doCleanups()


def test_execution_publishes_pr_without_claiming_ci_passed(scenario):
    from tests.bot_fixtures import TelegramTestCase

    case = TelegramTestCase()
    case.setUp()
    try:
        from ai_agent.bot.context_execution import lifecycle_execution

        spec, task, data = scenario
        task["stage"] = "implementing"
        item = {"context_spec": spec, "branch_name": "feature/a"}

        @lifecycle_execution
        async def execute(update, context, item):
            journal.checkpoint("commit", sha="a" * 40)
            journal.checkpoint("pr", url="https://github.com/owner/repo/pull/1")
            journal.checkpoint("ci", state="not_configured", sha="a" * 40)

        asyncio.run(execute(None, SimpleNamespace(user_data=data), item))
        assert task["stage"] == "pr"
        assert task["context_handoff"]
        assert task["pr_url"].endswith("/pull/1")
        assert not data.get("context_recovery_pending")
    finally:
        case.tearDown()
        case.doCleanups()
