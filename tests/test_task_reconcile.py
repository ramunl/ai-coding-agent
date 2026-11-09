"""Merged work recovers without duplicating an implementation or bypassing admission."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_agent import task_reconcile as recovery
from ai_agent.context_models import (
    AttemptSnapshot,
    EvidenceReference,
    RepositoryIdentity,
    Scope,
)


@pytest.fixture
def scenario(monkeypatch):
    scope = Scope(
        str(recovery.CHAT_ID), RepositoryIdentity("repo", "/repo", "owner/repo")
    )
    ref = {
        "kind": "attempt",
        "record_id": "archived",
        "revision": 1,
        "scope": asdict(scope),
    }
    record = AttemptSnapshot(
        record_id="archived",
        revision=1,
        scope=scope,
        summary="Observed attempt",
        task_id="11111111",
        attempt_id="archived",
        outcome="failed",
        evidence=(
            EvidenceReference("commit", "commit", "a" * 40, "Observed commit"),
            EvidenceReference(
                "pr",
                "pull_request",
                "https://github.com/owner/repo/pull/1",
                "Observed PR",
            ),
        ),
    )
    task = {
        "id": "11111111",
        "title": "Part one",
        "repo": "repo",
        "stage": "todo",
        "attempt": 2,
        "context_handoffs": [ref],
        "attempt_history": [
            {"attempt": 1, "stage": "stopped", "branch": "feature/old"}
        ],
    }
    child = {
        "id": "22222222",
        "stage": "planned",
        "plan_id": "next",
        "after": task["id"],
        "dependencies": [
            {
                "task_id": task["id"],
                "scope": ref["scope"],
                "attempt": deepcopy(ref),
                "policy": "merged_in_checkout",
            }
        ],
        "dependency_invalidations": {task["id"]: "attempt reopened"},
        "dependency_check": {"status": "unknown"},
    }
    data = {
        "tasks": [task, child],
        "pending_plan": SimpleNamespace(id="next", approved=False),
    }
    monkeypatch.setattr(
        recovery,
        "get_project",
        lambda _: SimpleNamespace(
            github_repository="owner/repo", repo_path=Path("/repo")
        ),
    )
    monkeypatch.setattr(
        recovery, "ContextStore", lambda _: SimpleNamespace(get=lambda *args: record)
    )
    facts = {
        "merged": True,
        "merge_commit": "b" * 40,
        "merged_at": 100,
        "head": "a" * 40,
        "url": "https://github.com/owner/repo/pull/1",
    }
    return task, child, data, record, facts


def test_owner_restores_merged_attempt_and_exact_unstarted_dependency(scenario):
    task, child, data, record, facts = scenario
    with patch.object(recovery, "verified_pull", return_value=facts) as check:
        message = asyncio.run(recovery.reconcile(task, data))
    check.assert_called_once_with("owner/repo", facts["url"], {"a" * 40})
    assert task["stage"] == "done" and task["pr_url"] == facts["url"]
    assert task["attempt"] == 2 and task["attempt_history"][0]["attempt"] == 1
    assert "dependency_invalidations" not in child
    assert "dependency_check" not in child  # fresh evidence is still required
    assert child["stage"] == "planned"
    assert "1 unchanged" in message
    assert child["dependency_reviews"][0]["source"] == "reconcile_task"


@pytest.mark.parametrize(
    "change", ["new_run", "new_plan", "active", "queue", "planning", "recovery"]
)
def test_refuses_replacement_work_without_querying_github(scenario, change):
    task, child, data, record, facts = scenario
    if change == "new_run":
        task["run"] = {"id": 2}
    if change == "new_plan":
        task["plan_id"] = "new"
    if change == "active":
        data["active_execution"] = {"task_id": task["id"]}
    if change == "queue":
        data["task_queue"] = [{"branch_name": "feature/old"}]
    if change == "planning":
        data["planning_task"] = task["id"]
    if change == "recovery":
        data["context_recovery_pending"] = True
    before = deepcopy(task)
    with patch.object(recovery, "verified_pull") as check, pytest.raises(ValueError):
        asyncio.run(recovery.reconcile(task, data))
    assert task == before
    check.assert_not_called()


@pytest.mark.parametrize(
    "change", ["unmerged", "missing_merge", "offline", "wrong_task", "wrong_repo"]
)
def test_bad_or_missing_pr_evidence_leaves_all_tasks_unchanged(
    scenario, monkeypatch, change
):
    task, child, data, record, facts = scenario
    if change == "unmerged":
        facts["merged"] = False
    if change == "missing_merge":
        facts["merge_commit"] = None
    if change == "wrong_task":
        object.__setattr__(record, "task_id", "other")
    if change == "wrong_repo":
        task["repo"] = "other"
    before = deepcopy(data["tasks"])
    with (
        patch.object(
            recovery,
            "verified_pull",
            side_effect=ValueError("offline") if change == "offline" else None,
            return_value=facts,
        ),
        pytest.raises(ValueError),
    ):
        asyncio.run(recovery.reconcile(task, data))
    assert data["tasks"] == before


def test_async_dependency_change_refuses_stale_result(scenario):
    task, child, data, record, facts = scenario

    def changed(*args):
        child["dependencies"][0]["revision"] = 2
        return facts

    with (
        patch.object(recovery, "verified_pull", changed),
        pytest.raises(ValueError, match="changed during"),
    ):
        asyncio.run(recovery.reconcile(task, data))
    assert task["stage"] == "todo"


@pytest.mark.parametrize(
    "change", ["started", "different_ref", "approved", "other_reason"]
)
def test_does_not_rebind_changed_or_started_consumers(scenario, change):
    task, child, data, record, facts = scenario
    if change == "started":
        child["run"] = {"id": 2}
    if change == "different_ref":
        child["dependencies"][0]["attempt"]["record_id"] = "other"
    if change == "approved":
        data["pending_plan"].approved = True
    if change == "other_reason":
        child["dependency_invalidations"][task["id"]] = "manual block"
    with patch.object(recovery, "verified_pull", return_value=facts):
        asyncio.run(recovery.reconcile(task, data))
    assert child["dependency_invalidations"]
