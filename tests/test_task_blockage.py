"""Resolve one exact orphaned execution without weakening other recovery gates."""

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent import context_attempts as journal
from ai_agent import context_lifecycle as lifecycle
from ai_agent import task_blockage as resolution
from ai_agent.context_store import ContextStore
from ai_agent.tasks import view


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    factory = lambda scope, root=None: ContextStore(scope, root or tmp_path / "context")
    monkeypatch.setattr(journal, "ContextStore", factory)
    monkeypatch.setattr(lifecycle, "ContextStore", factory)
    monkeypatch.setattr(resolution, "ContextStore", factory)
    monkeypatch.setattr(resolution, "CHAT_ID", 123)
    identity = {
        "project_name": "repo",
        "repo_path": str(tmp_path / "repo"),
        "github_repository": "owner/repo",
    }
    monkeypatch.setattr(
        resolution,
        "get_project",
        lambda _: SimpleNamespace(
            repo_path=Path(identity["repo_path"]), github_repository="owner/repo"
        ),
    )
    monkeypatch.setattr(resolution, "_recovery_lease", lambda _: nullcontext())
    spec = lifecycle.new_spec("123", identity, "11111111")
    task = {
        "id": "11111111",
        "repo": "repo",
        "title": "PM bridge",
        "stage": "ended",
        "branch": "feature/a",
        "run": {"context_spec": spec},
        "pr_url": "https://github.com/owner/repo/pull/1",
        "attempt": 1,
        "recovery_refs": ["refs/saved/work"],
    }
    child = {"id": "22222222", "stage": "todo", "after": task["id"]}
    data = {"tasks": [task, child], "context_recovery_pending": True}
    attempt = journal.Attempt(spec, task["branch"], data)
    attempt.save()
    attempt.checkpoint("commit", {"sha": "a" * 40})
    attempt.checkpoint("pr", {"sha": "a" * 40, "url": task["pr_url"], "number": 1})
    attempt.checkpoint(
        "ci",
        {
            "state": "running",
            "sha": "a" * 40,
            "url": "https://github.com/owner/repo/actions/runs/1",
        },
    )
    facts = {
        "merged": True,
        "head": "a" * 40,
        "merge_commit": "b" * 40,
        "merged_at": 100,
        "url": task["pr_url"],
    }
    proof = {
        "original_runs": ["https://github.com/owner/repo/actions/runs/1"],
        "verified_checks": [
            {"url": "https://github.com/owner/repo/actions/runs/2", "sha": "c" * 40}
        ],
        "branch": "main",
    }
    monkeypatch.setattr(resolution, "pull_facts", lambda *args: deepcopy(facts))
    monkeypatch.setattr(resolution, "superseding_checks", lambda *args: deepcopy(proof))
    return data, task, child, attempt, facts, proof, factory


def test_resolution_archives_new_proof_and_preserves_original_ci_and_refs(scenario):
    data, task, child, attempt, facts, proof, factory = scenario
    original = deepcopy(attempt.state["events"])
    result = asyncio.run(resolution.resolve(task, data))
    assert "Deployment is not asserted" in result
    assert task["stage"] == "done"
    assert task["merge_commit"] == "b" * 40
    assert task["recovery_refs"] == ["refs/saved/work"]
    assert not data["context_recovery_pending"]
    assert child == {"id": "22222222", "stage": "todo", "after": task["id"]}
    record = attempt.store.get("attempt", attempt.state["attempt_id"])
    assert record.outcome == "succeeded"
    assert "Original CI unresolved" in record.summary
    state = resolution._state(task)
    assert all(state["events"][k] == v for k, v in original.items())
    assert resolution._latest(state, "ci")["state"] == "running"
    assert resolution._latest(state, "ci_resolution")["state"] == "superseded"
    assert "already resolved" in asyncio.run(resolution.resolve(task, data))


def test_other_unfinished_attempt_keeps_global_gate(scenario):
    data, task, _, _, _, _, _ = scenario
    spec = lifecycle.new_spec(
        "123", task["run"]["context_spec"]["scope"]["repository"], "33333333"
    )
    other = journal.Attempt(spec, "feature/other", data)
    other.save()
    before = deepcopy(other.state)
    asyncio.run(resolution.resolve(task, data))
    assert data["context_recovery_pending"]
    assert other.state == before


@pytest.mark.parametrize(
    "change", ["open", "wrong_head", "active", "queued", "scope", "no_pr"]
)
def test_refusal_leaves_journal_and_task_untouched(scenario, change):
    data, task, _, attempt, facts, _, _ = scenario
    if change == "open":
        facts["merged"] = False
    if change == "wrong_head":
        facts["head"] = "d" * 40
    if change == "active":
        data["active_execution"] = {"branch": "feature/a"}
    if change == "queued":
        data["task_queue"] = [{"branch_name": "feature/a"}]
    if change == "scope":
        task["run"]["context_spec"]["scope"]["owner_id"] = "456"
    if change == "no_pr":
        task["pr_url"] = None
    before = deepcopy(data)
    with pytest.raises(ValueError):
        asyncio.run(resolution.resolve(task, data))
    assert data == before
    assert journal.unfinished(attempt.store)


def test_concurrent_task_or_journal_change_is_refused(scenario, monkeypatch):
    data, task, _, attempt, facts, _, _ = scenario

    def changed(*args):
        task["branch"] = "feature/new"
        return deepcopy(facts)

    monkeypatch.setattr(resolution, "pull_facts", changed)
    with pytest.raises(ValueError):
        asyncio.run(resolution.resolve(task, data))
    assert task["stage"] == "ended"
    assert journal.unfinished(attempt.store)


def test_archive_failure_keeps_recovery_pending_and_can_be_retried(
    scenario, monkeypatch
):
    data, task, _, attempt, _, _, _ = scenario
    original = journal.Attempt.finish
    monkeypatch.setattr(journal.Attempt, "finish", lambda *a: None)
    with pytest.raises(ValueError, match="archived"):
        asyncio.run(resolution.resolve(task, data))
    assert data["context_recovery_pending"]
    assert task["stage"] == "ended"
    monkeypatch.setattr(journal.Attempt, "finish", original)
    asyncio.run(resolution.resolve(task, data))
    assert task["stage"] == "done"


def test_replay_after_archive_before_task_persistence_recovers_same_attempt(scenario):
    data, task, _, _, _, _, _ = scenario
    saved = deepcopy(task)
    asyncio.run(resolution.resolve(task, data))
    task.clear()
    task.update(saved)
    data["context_recovery_pending"] = False
    asyncio.run(resolution.resolve(task, data))
    assert task["stage"] == "done"


def test_snapshot_advertises_check_without_claiming_resolution(scenario):
    data, task, *_ = scenario
    public = view(task, data)
    assert public["can_resolve_blockage"] is True
    assert public["blockage_resolved"] is False
    task["stage"] = "implementing"
    assert view(task, data)["can_resolve_blockage"] is False


def test_changed_journal_and_writer_lease_prevent_completion(scenario, monkeypatch):
    data, task, _, attempt, facts, _, _ = scenario

    def changed(*args):
        attempt.checkpoint("ci", {"state": "running", "url": "new observation"})
        return deepcopy(facts)

    monkeypatch.setattr(resolution, "pull_facts", changed)
    with pytest.raises(ValueError, match="evidence changed"):
        asyncio.run(resolution.resolve(task, data))
    assert task["stage"] == "ended"
    monkeypatch.setattr(resolution, "pull_facts", lambda *args: deepcopy(facts))

    def busy(*args):
        raise RuntimeError("Checkout still has an active writer")

    monkeypatch.setattr(resolution, "_recovery_lease", busy)
    with pytest.raises(RuntimeError, match="active writer"):
        asyncio.run(resolution.resolve(task, data))
    assert task["stage"] == "ended"
    assert data["context_recovery_pending"]
