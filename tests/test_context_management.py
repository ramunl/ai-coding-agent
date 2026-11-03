"""Management boundaries use real temporary archives and store locks."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ai_agent.context_management import (
    MAX_RESPONSE_BYTES,
    ContextManagement,
    ManagementError,
    OwnerEvent,
)
from ai_agent.context_models import (
    AttemptSnapshot,
    Decision,
    OwnerContext,
    RecordReference,
    RepositoryIdentity,
    Scope,
)
from ai_agent.context_store import (
    ContextPersistenceError,
    ContextStore,
    RevisionConflictError,
)
from ai_agent.context_validation import ScopeMismatchError


@pytest.fixture
def archive(tmp_path):
    scope = Scope("123")
    store = ContextStore(scope, tmp_path / "archive")
    record = store.write(
        OwnerContext(
            record_id="notes",
            scope=scope,
            summary="Notes",
            decisions=(Decision("choice", "Use tests"),),
        ),
        expected_revision=0,
    )
    service = ContextManagement(store, "123", OwnerEvent("123", "123", 42))
    return store, service, RecordReference("owner", record.record_id, 1, scope)


def test_approval_and_rejection_preserve_history(archive):
    store, service, ref = archive
    rejected = service.decide(ref, "choice", approve=False)
    assert rejected.decisions == ()
    assert store.get("owner", "notes", 1).decisions[0].status == "proposed"
    with pytest.raises(RevisionConflictError):
        service.decide(ref, "choice", approve=True)
    restored = store.write(
        replace(rejected, decisions=(Decision("new", "Yes"),)), expected_revision=2
    )
    approved = service.decide(
        replace(ref, revision=restored.revision), "new", approve=True
    )
    approval = approved.decisions[0].approval
    assert (approval.owner_id, approval.event_id, approval.channel) == (
        "123",
        "telegram-42",
        "telegram",
    )
    assert approval.approved_at > 0
    for approve in (True, False):
        with pytest.raises(ManagementError):
            service.decide(replace(ref, revision=4), "new", approve=approve)


def test_owner_event_required_before_access(archive):
    store, _, _ = archive
    for event in (
        OwnerEvent("other", "123", 1),
        OwnerEvent("123", "other", 1),
        OwnerEvent("123", "123", -1),
    ):
        with pytest.raises(ManagementError):
            ContextManagement(store, "123", event)
    with pytest.raises(ManagementError):
        ContextManagement(store, "other", OwnerEvent("other", "other", 1))


def test_link_exact_scope_and_revision(archive):
    store, service, ref = archive
    target = store.write(
        OwnerContext(record_id="target", scope=ref.scope, summary="target"),
        expected_revision=0,
    )
    target_ref = replace(ref, record_id=target.record_id)
    linked = service.link(ref, target_ref)
    assert linked.references == (target_ref,)
    with pytest.raises(RevisionConflictError):
        service.link(ref, target_ref)
    with pytest.raises(ScopeMismatchError):
        service.link(
            replace(ref, revision=2), replace(target_ref, scope=Scope("other"))
        )
    with pytest.raises(ContextPersistenceError):
        service.link(replace(ref, revision=2), replace(target_ref, revision=99))
    assert store.get("owner", "notes").revision == 2


def test_inspection_bounds_and_redaction(archive):
    store, service, ref = archive
    record = store.get("owner", "notes")
    store.write(
        replace(
            record,
            summary="token=private /home/private/file C:\\private\\file " + "x" * 1000,
        ),
        expected_revision=1,
    )
    result = service.inspect((replace(ref, revision=2),) * 20)
    assert len(result.encode()) <= MAX_RESPONSE_BYTES
    parsed = json.loads(result)
    assert parsed["truncated"] and len(parsed["records"]) <= 4
    assert "private" not in result and "repo_path" not in result
    assert "[redacted]" in result and "[path]" in result
    assert "approval" not in result and "evidence" not in result
    assert json.loads(service.inspect((ref,)))["records"][0]["summary"] == "Notes"


def test_unknown_and_unavailable_archive_do_not_create_records(archive):
    store, service, ref = archive
    with pytest.raises(ContextPersistenceError):
        service.decide(replace(ref, record_id="unknown"), "choice", approve=True)
    assert not list(store.root.rglob("*unknown*"))
    path = store.root / "123" / "owner.notes.1.json"
    path.unlink()
    path.symlink_to("/unavailable")
    with pytest.raises(ContextPersistenceError):
        service.inspect((ref,))


def test_concurrent_decisions_have_one_winner(archive):
    store, service, ref = archive

    def write(approve):
        try:
            return service.decide(ref, "choice", approve=approve).revision
        except RevisionConflictError:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, (True, False)))
    assert sorted(map(str, results)) == ["2", "conflict"]
    assert store.get("owner", "notes").revision == 2


def test_attempts_remain_immutable(tmp_path):
    scope = Scope("123", RepositoryIdentity("repo", "/private/repo", "org/repo"))
    store = ContextStore(scope, tmp_path / "archive")
    store.write(
        AttemptSnapshot(
            record_id="attempt",
            scope=scope,
            summary="done",
            task_id="task",
            attempt_id="attempt",
            outcome="succeeded",
            decisions=(Decision("choice", "proposal"),),
        ),
        expected_revision=0,
    )
    service = ContextManagement(store, "123", OwnerEvent("123", "123", 1))
    ref = RecordReference("attempt", "attempt", 1, scope)
    with pytest.raises(ManagementError, match="immutable"):
        service.decide(ref, "choice", approve=False)
    with pytest.raises(ManagementError, match="immutable"):
        service.link(ref, ref)


def test_response_byte_budget_and_item_limit(archive):
    store, service, ref = archive
    record = store.get("owner", "notes")
    store.write(
        replace(
            record, decisions=tuple(Decision(f"d{i}", "☃" * 300) for i in range(20))
        ),
        expected_revision=1,
    )
    response = service.inspect((replace(ref, revision=2),) * 4)
    assert len(response.encode()) <= MAX_RESPONSE_BYTES
    assert json.loads(response)["truncated"]


def test_archive_scan_limit_still_blocks_mutations(archive, monkeypatch):
    from ai_agent import context_store

    store, service, ref = archive
    monkeypatch.setattr(context_store, "MAX_SCAN_ENTRIES", 0)
    with pytest.raises(ContextPersistenceError, match="scan limit"):
        service.decide(ref, "choice", approve=True)
    assert not (store.root / "123" / "owner.notes.2.json").exists()


def test_same_owner_different_repository_is_not_authorized(archive):
    _, service, ref = archive
    wrong = Scope("123", RepositoryIdentity("repo", "/tmp/repo", "org/repo"))
    with pytest.raises(ScopeMismatchError):
        service.inspect((replace(ref, scope=wrong),))
