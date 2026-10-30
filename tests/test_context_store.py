"""Durability, recovery and retention tests confined to temporary archives."""

import json
import multiprocessing
import os
from dataclasses import asdict, replace

import pytest

from ai_agent import context_index, context_store
from ai_agent.context_models import (
    AttemptSnapshot,
    OwnerContext,
    RecordReference,
    RepositoryContext,
    RepositoryIdentity,
    Scope,
)
from ai_agent.context_store import (
    ContextPersistenceError,
    ContextStore,
    RevisionConflictError,
)
from ai_agent.context_validation import ContextValidationError, UnsupportedSchemaError


@pytest.fixture
def archive(tmp_path):
    store = ContextStore(Scope("owner1"), tmp_path / "context")
    record = OwnerContext(record_id="record1", scope=store.scope, summary="safe")
    return store, record


def _concurrent_write(root, ready, start, results):
    store = ContextStore(Scope("owner1"), root)
    record = store.get("owner", "record1")
    ready.put(True)
    start.wait(10)
    try:
        store.write(replace(record, summary="changed"), expected_revision=1)
        results.put("written")
    except RevisionConflictError:
        results.put("conflict")


def test_concurrent_processes_compare_revision_under_lock(archive):
    store, record = archive
    store.write(record, expected_revision=0)
    ctx = multiprocessing.get_context("fork")
    ready, results, start = ctx.Queue(), ctx.Queue(), ctx.Event()
    processes = [
        ctx.Process(target=_concurrent_write, args=(store.root, ready, start, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        for _ in processes:
            assert ready.get(timeout=10)
        start.set()
        assert sorted(results.get(timeout=10) for _ in processes) == [
            "conflict",
            "written",
        ]
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join()
    assert all(process.exitcode == 0 for process in processes)
    assert store.get("owner", "record1").revision == 2
    assert store.get("owner", "record1", 1).summary == "safe"


def test_stale_revisions_permissions_and_references(archive):
    store, draft = archive
    first = store.write(draft, expected_revision=0)
    with pytest.raises(RevisionConflictError):
        store.write(draft, expected_revision=0)
    second = store.write(replace(first, summary="new"), expected_revision=1)
    reference = RecordReference("owner", draft.record_id, 1, store.scope)
    linked = replace(draft, record_id="linked", references=(reference,))
    store.write(linked, expected_revision=0)
    assert store.get("owner", "record1", 1) == first
    assert second.revision == 2
    for path in (store.root, store.root / "owner1"):
        assert path.stat().st_mode & 0o777 == 0o700
    for path in (store.root / "owner1").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ContextPersistenceError):
        store.write(
            replace(
                linked,
                record_id="missing",
                references=(replace(reference, revision=99),),
            ),
            expected_revision=0,
        )


def test_immutable_attempt_and_packet_scope(tmp_path):
    scope = Scope("owner1", RepositoryIdentity("repo", "/tmp/repo", "org/repo"))
    store = ContextStore(scope, tmp_path / "context")
    record = AttemptSnapshot(
        record_id="attempt1",
        scope=scope,
        summary="Tests failed",
        task_id="task1",
        attempt_id="try1",
        outcome="failed",
    )
    saved = store.write(record, expected_revision=0)
    with pytest.raises(RevisionConflictError):
        store.write(saved, expected_revision=1)
    other = ContextStore(
        replace(scope, repository=replace(scope.repository, repo_path="/tmp/other")),
        store.root,
    )
    with pytest.raises(ContextValidationError):
        other.get("attempt", "attempt1")
    with pytest.raises(ContextValidationError):
        other.write(record, expected_revision=0)
    repo = RepositoryContext(record_id="repo1", scope=scope, summary="Notes")
    store.write(repo, expected_revision=0)


@pytest.mark.parametrize("failure", ["replace", "file_fsync", "directory_fsync"])
def test_interrupted_commit_is_old_or_complete(archive, monkeypatch, failure):
    store, draft = archive
    saved = store.write(draft, expected_revision=0)
    real_fsync = os.fsync
    calls = 0

    def fail_sync(fd):
        nonlocal calls
        calls += 1
        if calls == (1 if failure == "file_fsync" else 2):
            raise OSError("simulated crash")
        return real_fsync(fd)

    def fail_replace(*args, **kwargs):
        raise OSError("simulated crash")

    with monkeypatch.context() as patch:
        if failure == "replace":
            patch.setattr(context_store.os, "replace", fail_replace)
        else:
            patch.setattr(context_store.os, "fsync", fail_sync)
        with pytest.raises(ContextPersistenceError):
            store.write(replace(saved, summary="new"), expected_revision=1)
    current = store.get("owner", "record1")
    assert current.revision == (2 if failure == "directory_fsync" else 1)
    assert store.get("owner", "record1", 1) == saved
    assert not list((store.root / "owner1").glob(".pending-*"))


def test_rebuild_preserves_corrupt_unknown_and_crash_leftovers(archive):
    store, draft = archive
    store.write(draft, expected_revision=0)
    directory = store.root / "owner1"
    corrupt = directory / "owner.broken.1.json"
    corrupt.write_bytes(b'{"partial":')
    unknown = directory / "owner.future.1.json"
    unknown.write_text(json.dumps({**asdict(draft), "schema_version": 2}))
    leftover = directory / ".pending-crash"
    leftover.write_text("partial")
    before = {path: path.read_bytes() for path in (corrupt, unknown, leftover)}
    result = context_index.rebuild_index(store)
    assert not result.complete
    assert len(result.entries) == 1
    assert len(result.diagnostics) == 2
    assert all(path.read_bytes() == content for path, content in before.items())
    with pytest.raises(UnsupportedSchemaError):
        store.get("owner", "future")
    with pytest.raises(ContextValidationError):
        store.get("owner", "broken")
    (directory / "index.json").write_text("broken cache")
    assert context_index.load_index(store) == result
    # Existing unsupported data must never be replaced even with a matching revision.
    with pytest.raises(UnsupportedSchemaError):
        store.write(replace(draft, record_id="future", revision=1), expected_revision=1)
    assert unknown.read_bytes() == before[unknown]


def test_index_failure_and_bounds_leave_records_authoritative(archive, monkeypatch):
    store, draft = archive
    saved = store.write(draft, expected_revision=0)

    def fail(*args):
        raise OSError("index disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(context_index, "atomic_write", fail)
        with pytest.raises(ContextPersistenceError):
            context_index.rebuild_index(store)
    assert store.get("owner", "record1") == saved
    monkeypatch.setattr(context_index, "MAX_INDEX_ENTRIES", 0)
    result = context_index.rebuild_index(store)
    assert not result.complete and not result.entries
    monkeypatch.setattr(context_store, "MAX_SCAN_ENTRIES", 0)
    assert not context_index.rebuild_index(store).complete


@pytest.mark.parametrize("location", ["root", "ancestor", "owner", "record", "lock"])
def test_symlink_escapes_are_rejected(tmp_path, location):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")
    root = tmp_path / "context"
    if location in {"root", "ancestor"}:
        root.symlink_to(outside, target_is_directory=True)
        if location == "ancestor":
            root = root / "nested"
    else:
        root.mkdir()
        owner = root / "owner1"
        if location == "owner":
            owner.symlink_to(outside, target_is_directory=True)
        else:
            owner.mkdir()
            name = (
                "owner.record1.1.json" if location == "record" else "owner.record1.lock"
            )
            (owner / name).symlink_to(sentinel)
    store = ContextStore(Scope("owner1"), root)
    draft = OwnerContext(record_id="record1", scope=store.scope, summary="safe")
    with pytest.raises(ContextPersistenceError):
        if location == "record":
            store.get("owner", "record1")
        else:
            store.write(draft, expected_revision=0)
    assert sentinel.read_text() == "unchanged"


def test_paths_size_redaction_and_retention(archive, monkeypatch):
    from ai_agent import context_validation, tasks

    store, draft = archive
    for path in (store.root / ".." / "escape", "relative", "/"):
        with pytest.raises(ContextPersistenceError):
            ContextStore(store.scope, path)
    with pytest.raises(ContextValidationError):
        store.get("owner", "../escape")
    monkeypatch.setattr(
        context_validation,
        "redact_sensitive",
        lambda text: text.replace("fake-secret", "[redacted]"),
    )
    saved = store.write(
        replace(draft, summary="fake-secret password=hunter2"), expected_revision=0
    )
    path = store.root / "owner1" / "owner.record1.1.json"
    assert "fake-secret" not in path.read_text() and "hunter2" not in path.read_text()
    # Legacy tasks need no archive keys, and pruning never touches archived references.
    data = {"tasks": [{"id": "legacy", "stage": "done"}]}
    assert tasks.tasks_of(data)[0]["images"] == []
    ref = RecordReference("owner", "record1", 1, store.scope)
    store.write(
        replace(draft, record_id="linked", references=(ref,)), expected_revision=0
    )
    for number in range(55):
        task = tasks.add_task(data, f"Task {number}", "repo", None)
        task["stage"] = "done"
    assert len(tasks.tasks_of(data)) == 50
    assert tasks.find_task(data, "legacy") is None
    assert store.get("owner", "record1", 1) == saved
    path.write_bytes(b"x" * (context_store.MAX_RECORD_BYTES + 1))
    with pytest.raises(ContextValidationError):
        store.get("owner", "record1")


def test_unsupported_index_is_preserved_and_other_scopes_are_filtered(archive):
    store, draft = archive
    store.write(draft, expected_revision=0)
    scope = Scope("owner1", RepositoryIdentity("repo", "/tmp/repo", "org/repo"))
    other = ContextStore(scope, store.root)
    other.write(
        RepositoryContext(record_id="repo1", scope=scope, summary="Notes"),
        expected_revision=0,
    )
    result = context_index.rebuild_index(store)
    assert result.complete and len(result.entries) == 1
    path = store.root / "owner1" / "index.json"
    future = b'{"schema_version": 99, "opaque": "preserve"}'
    path.write_bytes(future)
    rebuilt = context_index.load_index(store)
    assert len(rebuilt.entries) == 1 and not rebuilt.complete
    assert "Unsupported index schema preserved" in rebuilt.diagnostics
    assert path.read_bytes() == future


def test_corrupt_latest_does_not_fall_back_or_allow_overwrite(archive):
    store, draft = archive
    first = store.write(draft, expected_revision=0)
    path = store.root / "owner1" / "owner.record1.2.json"
    path.write_bytes(b"{crash")
    with pytest.raises(ContextValidationError):
        store.get("owner", "record1")
    with pytest.raises(ContextValidationError):
        store.write(replace(first, revision=2), expected_revision=2)
    assert store.get("owner", "record1", 1) == first
    assert path.read_bytes() == b"{crash"


@pytest.mark.parametrize("entry_type", ["hardlink", "fifo"])
def test_nonprivate_record_files_are_rejected(archive, entry_type, tmp_path):
    store, draft = archive
    store.write(draft, expected_revision=0)
    path = store.root / "owner1" / "owner.record1.1.json"
    if entry_type == "hardlink":
        os.link(path, tmp_path / "linked")
    else:
        path.unlink()
        os.mkfifo(path)
    with pytest.raises(ContextPersistenceError):
        store.get("owner", "record1")


def test_packet_references_are_pinned_and_checked_before_commit(tmp_path):
    from ai_agent.context_models import PacketReference

    scope = Scope("owner1", RepositoryIdentity("repo", "/tmp/repo", "org/repo"))
    store = ContextStore(scope, tmp_path / "context")
    repo = store.write(
        RepositoryContext(record_id="repo1", scope=scope, summary="Notes"),
        expected_revision=0,
    )
    ref = RecordReference("repository", "repo1", 1, scope)
    packet = PacketReference("packet1", "a" * 64, (ref,))
    attempt = AttemptSnapshot(
        record_id="attempt1",
        scope=scope,
        summary="Result",
        task_id="task1",
        attempt_id="try1",
        outcome="succeeded",
        packets=(packet,),
    )
    saved = store.write(attempt, expected_revision=0)
    store.write(replace(repo, summary="Updated"), expected_revision=1)
    assert (
        store.get("repository", "repo1", saved.packets[0].records[0].revision) == repo
    )
    bad = replace(
        attempt,
        record_id="attempt2",
        packets=(replace(packet, records=(replace(ref, revision=99),)),),
    )
    with pytest.raises(ContextPersistenceError):
        store.write(bad, expected_revision=0)
    with pytest.raises(ContextPersistenceError):
        store.get("attempt", "attempt2")
    bad_scope = replace(ref, scope=replace(scope, owner_id="another"))
    with pytest.raises(ContextValidationError):
        store.write(
            replace(bad, packets=(replace(packet, records=(bad_scope,)),)),
            expected_revision=0,
        )


def test_sensitive_scope_cannot_reach_even_an_empty_index(tmp_path, monkeypatch):
    from ai_agent import context_validation

    monkeypatch.setattr(
        context_validation,
        "redact_sensitive",
        lambda text: text.replace("configured-secret", "[redacted]"),
    )
    for path in ("/tmp/configured-secret", "/tmp/password=private"):
        scope = Scope("owner1", RepositoryIdentity("repo", path, "org/repo"))
        with pytest.raises(ContextValidationError, match="Sensitive scope"):
            context_index.rebuild_index(ContextStore(scope, tmp_path / "context"))
    assert not (tmp_path / "context").exists()
