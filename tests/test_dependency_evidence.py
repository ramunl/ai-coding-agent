"""Dependency regressions: display stages cannot authorize execution."""

import pytest

from ai_agent.tasks import waiting_for


@pytest.mark.parametrize("stage", ["pr", "done", None])
def test_cached_stage_or_missing_predecessor_blocks(stage):
    predecessor = {"id": "first", "title": "First", "stage": stage}
    data = {"tasks": [predecessor] if stage else []}
    assert waiting_for({"id": "second", "after": "first"}, data) is not None


@pytest.mark.parametrize("value", ["first", 12, {}, [None]])
def test_malformed_legacy_dependencies_block(value):
    assert waiting_for({"dependency_ids": value}, {"tasks": []}) is not None


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    import subprocess
    from dataclasses import asdict

    from ai_agent import dependency_evidence as gate
    from ai_agent.context_models import (
        AttemptSnapshot,
        Dependency,
        EvidenceReference,
        RecordReference,
        RepositoryIdentity,
        Scope,
    )
    from ai_agent.context_store import ContextStore

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.org")
    git("config", "user.name", "Test")
    git("remote", "add", "origin", "https://github.com/owner/predecessor.git")
    git("commit", "--allow-empty", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "squash merge")
    merged = git("rev-parse", "HEAD")
    scope = Scope(
        str(gate.CHAT_ID),
        RepositoryIdentity("predecessor", str(repo), "owner/predecessor"),
    )
    store = ContextStore(scope, tmp_path / "archive")
    head = "a" * 40  # Original feature head need not be an ancestor after squash.
    url = "https://github.com/owner/predecessor/pull/12"
    record = store.write(
        AttemptSnapshot(
            record_id="attempt1",
            scope=scope,
            summary="application observations",
            task_id="first",
            attempt_id="attempt1",
            outcome="succeeded",
            evidence=(
                EvidenceReference("pr", "pull_request", url, "PR"),
                EvidenceReference("commit", "commit", head, "commit"),
            ),
        ),
        expected_revision=0,
    )
    ref = RecordReference("attempt", record.record_id, record.revision, scope)
    dependency = asdict(Dependency("first", scope, ref))
    pull = {
        "merged": True,
        "reviewable": False,
        "merge_commit": merged,
        "checked_at": gate.time.time(),
    }
    from ai_agent import task_pulls

    monkeypatch.setattr(task_pulls, "verified_pull", lambda *args: dict(pull))
    return gate, dependency, store, pull, git, base


def test_squash_merge_uses_predecessor_checkout_and_rechecks_head(evidence):
    gate, dependency, store, pull, git, base = evidence
    assert (
        gate.evaluate_dependency(dependency, store=store)["checkout_head"]
        == pull["merge_commit"]
    )
    git("checkout", "--detach", base)
    with pytest.raises(ValueError, match="Checkout evidence unavailable"):
        gate.evaluate_dependency(dependency, store=store)


@pytest.mark.parametrize("change", ["open", "missing_commit", "stale"])
def test_incomplete_or_unmerged_evidence_blocks(evidence, change):
    gate, dependency, store, pull, _, _ = evidence
    if change == "open":
        pull.update(merged=False, reviewable=True)
    elif change == "missing_commit":
        pull["merge_commit"] = "b" * 40
    else:
        pull["checked_at"] -= 61
    with pytest.raises(ValueError):
        gate.evaluate_dependency(dependency, store=store)


def test_reviewable_is_explicit_opt_in(evidence):
    gate, dependency, store, pull, _, _ = evidence
    dependency["policy"] = "reviewable_pr"
    pull.update(merged=False, reviewable=True)
    assert gate.evaluate_dependency(dependency, store=store)["pull"]["reviewable"]
    pull["reviewable"] = False
    with pytest.raises(ValueError):
        gate.evaluate_dependency(dependency, store=store)


def test_archive_survives_task_eviction(evidence, monkeypatch):
    import asyncio

    from ai_agent import tasks

    gate, dependency, store, _, _, _ = evidence
    monkeypatch.setattr(gate, "ContextStore", lambda scope: store)
    first = {"id": "first", "stage": "done", "context_handoff": dependency["attempt"]}
    consumer = {"id": "second", "after": "first", "stage": "todo"}
    data = {"tasks": [first, consumer]}
    monkeypatch.setattr(tasks, "MAX_TASKS", 2)
    tasks.add_task(data, "New", "other", None)
    assert first not in data["tasks"]
    assert consumer["dependencies"][0]["attempt"] == dependency["attempt"]
    assert asyncio.run(gate.refresh(consumer, data)) is None
    consumer["dependencies"][0]["revision"] = 2
    assert gate.cached_reason(consumer, data)
    consumer["dependencies"][0]["attempt"]["record_id"] = "missing"
    assert asyncio.run(gate.refresh(consumer, data))


def test_attempt_change_invalidates_even_with_cached_done(evidence):
    import asyncio

    gate, dependency, _, _, _, _ = evidence
    task = {"dependencies": [dependency]}
    data = {
        "tasks": [
            {
                "id": "first",
                "stage": "done",
                "context_handoff": {**dependency["attempt"], "record_id": "attempt2"},
            }
        ]
    }
    assert "attempt changed" in asyncio.run(gate.refresh(task, data))


def test_audited_override_is_bound_to_revision_and_policy(evidence):
    from dataclasses import asdict

    from ai_agent.context_models import Approval
    from ai_agent.context_validation import decode_dependency
    from ai_agent.dependency_overrides import approve_override

    gate, value, store, _, _, _ = evidence
    dependency = decode_dependency(value, str(gate.CHAT_ID))
    ref = approve_override(
        dependency,
        Approval(str(gate.CHAT_ID), "owner_event", 1, "manual"),
        "Owner reviewed the exception",
        store,
    )
    value["override"] = asdict(ref)
    assert gate.evaluate_dependency(value, store=store)["override"] == asdict(ref)
    value["revision"] = 2
    with pytest.raises(ValueError, match="stale or unaudited"):
        gate.evaluate_dependency(value, store=store)


def test_context_packet_gates_cross_repository_in_its_own_checkout(
    evidence, monkeypatch
):
    from dataclasses import replace

    from ai_agent.context_models import RepositoryIdentity
    from ai_agent.context_packets import build_packet
    from ai_agent.context_sources import ContextBlockedError, ContextRequest
    from ai_agent.context_validation import decode_dependency

    gate, value, store, pull, _, _ = evidence
    dependency = decode_dependency(value, str(gate.CHAT_ID))
    consumer_scope = replace(
        dependency.scope,
        repository=RepositoryIdentity("consumer", "/not-a-checkout", "owner/consumer"),
    )
    request = ContextRequest(
        scope=consumer_scope,
        authorized_owner_id=consumer_scope.owner_id,
        initiative_id="consumer",
        task_id="consumer",
        attempt_id="new",
        stores=(),
        dependencies=(dependency,),
    )
    monkeypatch.setattr(gate, "ContextStore", lambda scope: store)
    assert '"policy":"merged_in_checkout"' in build_packet(request).text
    pull.update(merged=False, reviewable=True)
    with pytest.raises(ContextBlockedError, match="Dependency evidence unknown"):
        build_packet(request)


def test_reopen_invalidation_survives_predecessor_removal(evidence, monkeypatch):
    import asyncio

    from ai_agent.task_reopen import reopen_task
    from ai_agent.tasks import remove_task

    gate, dependency, store, _, _, _ = evidence
    monkeypatch.setattr(gate, "ContextStore", lambda scope: store)
    predecessor = {
        "id": "first",
        "stage": "done",
        "context_handoff": dependency["attempt"],
    }
    task = {"id": "consumer", "dependencies": [dependency]}
    data = {"tasks": [predecessor, task]}
    assert asyncio.run(gate.refresh(task, data)) is None
    reopen_task(predecessor, data)
    remove_task(data, predecessor)
    assert asyncio.run(gate.refresh(task, data))
    assert task["dependency_invalidations"]


def test_old_context_records_decode_without_granting_readiness(evidence):
    from dataclasses import asdict

    from ai_agent.context_validation import decode_record

    _, _, store, _, _, _ = evidence
    old = asdict(store.get("attempt", "attempt1", 1))
    old.pop("dependencies")
    assert decode_record(old).dependencies == ()
