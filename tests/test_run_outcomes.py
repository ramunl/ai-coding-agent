"""Operational and blocked runs stay actionable without pretending to finish."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ai_agent.run_outcomes import NoCodeOutcomeError, record_outcome
from ai_agent.tasks import sync


def test_operational_report_is_persistent_and_task_stays_open():
    outcome = NoCodeOutcomeError(
        json.dumps(
            {
                "outcome": "ops_required",
                "summary": "Package source unavailable",
                "evidence": "HTTP 402",
                "next_step": "Review source in Ops",
            }
        )
    )
    task = {
        "id": "a",
        "branch": "bugfix/source",
        "stage": "implementing",
        "todo": "x:1",
    }
    data = {"tasks": [task]}
    record_outcome(data, task["branch"], outcome)
    sync(data)
    assert task["stage"] == "ops_required"
    assert "402" in task["note"]
    persistence = importlib.import_module("ai_agent.bot.persistence")
    saved = persistence.deserialize_user_data(persistence.serialize_user_data(data))
    assert saved["last_run_outcome"]["status"] == "ops_required"
    assert saved["tasks"][0]["todo"] == "x:1"


def test_unstructured_or_unrecognized_report_defaults_to_blocked():
    for report in ["Permission denied", '{"outcome":"done"}', "{broken"]:
        outcome = NoCodeOutcomeError(report)
        assert outcome.status == "blocked"
        assert outcome.report


def test_queue_reports_no_code_outcome_and_continues_without_ci():
    execution = importlib.import_module("ai_agent.bot.execution")
    outcome = execution.NoCodeOutcomeError('{"outcome":"ops_required","summary":"402"}')
    update = SimpleNamespace(effective_user=SimpleNamespace(id=123))
    data = {
        "task_queue": [
            {"id": 1, "branch_name": "bugfix/source"},
            {"id": 2, "branch_name": "bugfix/second"},
        ]
    }
    context = SimpleNamespace(user_data=data, application=Mock())
    with (
        patch.object(
            execution, "_publish_queued_implementation", AsyncMock(side_effect=outcome)
        ) as publish,
        patch.object(execution, "watch_ci", AsyncMock()) as ci,
        patch.object(execution, "reset_to_base_branch", AsyncMock()) as reset,
        patch.object(execution, "discard_empty_branch") as discard,
        patch.object(execution, "reply_chunks", AsyncMock()) as reply,
    ):
        asyncio.run(execution.run_task_queue(update, context))
    assert publish.await_count == 2
    assert reset.await_count == 2
    assert discard.call_count == 2
    ci.assert_not_awaited()
    assert "Needs Ops action" in reply.call_args.args[1]
    assert data["last_run_outcome"]["branch"] == "bugfix/second"
    assert not data.get("queue_runner_active")
    context.application.mark_data_for_update_persistence.assert_called()


def test_empty_branch_cleanup_keeps_any_branch_with_different_commit():
    from ai_agent.run_outcomes import discard_empty_branch
    from ai_agent.shell import CommandResult

    for branch_sha in ("base", "work"):
        replies = [
            CommandResult([], 0, branch_sha),
            CommandResult([], 0, "base"),
            CommandResult([], 0, ""),
        ]
        with (
            patch(
                "ai_agent.projects.active_project",
                return_value=SimpleNamespace(base_branch="main"),
            ),
            patch("ai_agent.shell.run", side_effect=replies) as run,
        ):
            discard_empty_branch("bugfix/x")
        assert run.call_count == (3 if branch_sha == "base" else 2)
