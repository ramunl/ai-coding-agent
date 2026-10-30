"""Contract validation uses synthetic identities and no external providers."""

from dataclasses import asdict, replace

import pytest

from ai_agent.context_models import (
    Approval,
    AttemptSnapshot,
    Decision,
    EvidenceReference,
    InitiativeContext,
    OwnerContext,
    PacketReference,
    RecordReference,
    RepositoryContext,
    RepositoryIdentity,
    Scope,
)
from ai_agent.context_validation import (
    ContextValidationError,
    UnsupportedSchemaError,
    decode_record,
    sanitized_record,
    validate_transition,
)

OWNER = Scope("owner1")
REPO = Scope("owner1", RepositoryIdentity("example", "/tmp/example", "org/example"))


def test_contract_round_trips():
    for record in (
        OwnerContext(record_id="owner-context", scope=OWNER, summary="Preferences"),
        InitiativeContext(
            record_id="initiative1",
            scope=OWNER,
            summary="Goal",
            dependency_ids=("stable-dependency",),
        ),
        RepositoryContext(record_id="repo1", scope=REPO, summary="Architecture"),
        AttemptSnapshot(
            record_id="attempt1",
            scope=REPO,
            summary="Result",
            task_id="old-task",
            attempt_id="try1",
            outcome="failed",
            packets=(PacketReference("packet1", "a" * 64),),
        ),
    ):
        assert decode_record(asdict(record)) == record


@pytest.mark.parametrize(
    "update",
    [
        {"record_id": "../escape"},
        {"record_id": "name with spaces"},
        {"revision": True},
        {"revision": -1},
        {"summary": 123},
        {"summary": "x" * 4001},
        {"raw_prompt": "forbidden"},
        {"transcript": []},
        {"environment": {}},
        {"images": []},
        {"decisions": [{}] * 65},
        {"kind": []},
        {"scope": {"owner_id": "other/owner"}},
    ],
)
def test_rejects_malformed_contracts(update):
    data = asdict(OwnerContext(record_id="id", scope=OWNER, summary="safe"))
    data.update(update)
    with pytest.raises(ContextValidationError):
        decode_record(data)


def test_unknown_schema_and_total_size():
    record = OwnerContext(record_id="id", scope=OWNER, summary="safe")
    with pytest.raises(UnsupportedSchemaError):
        decode_record({**asdict(record), "schema_version": 2})
    evidence = tuple(
        EvidenceReference(f"e{i}", "test", "local", "x" * 4000) for i in range(20)
    )
    with pytest.raises(ContextValidationError, match="size"):
        sanitized_record(replace(record, evidence=evidence))


def test_approval_and_supersession():
    approval = Approval("owner1", "event1", 123, "telegram")
    old = Decision("d1", "Choice", "approved", approval)
    proposed = Decision("d2", "Replacement")
    record = OwnerContext(
        record_id="id", scope=OWNER, summary="safe", decisions=(old, proposed)
    )
    assert sanitized_record(record) == record
    invalid = [
        replace(old, approval=None),
        replace(old, approval=replace(approval, owner_id="another")),
        replace(old, approval=replace(approval, approved_at=0)),
        replace(old, status="proposed"),
        replace(old, status="superseded", superseded_by="missing"),
        replace(old, status="superseded", superseded_by="d2"),
        replace(old, status="superseded", superseded_by="d1"),
    ]
    for decision in invalid:
        with pytest.raises(ContextValidationError):
            sanitized_record(replace(record, decisions=(decision, proposed)))
    replacement = replace(proposed, status="approved", approval=approval)
    superseded = replace(old, status="superseded", superseded_by="d2")
    updated = replace(record, decisions=(superseded, replacement))
    assert sanitized_record(updated) == updated
    validate_transition(record, updated)
    for changed in (
        (),
        (replace(old, summary="Rewritten"),),
        (replace(old, status="proposed", approval=None),),
    ):
        with pytest.raises(ContextValidationError):
            validate_transition(record, replace(record, decisions=changed))
    with pytest.raises(ContextValidationError):
        sanitized_record(
            replace(
                updated,
                decisions=(
                    superseded,
                    replace(replacement, status="superseded", superseded_by="d1"),
                ),
            )
        )


def test_scopes_references_and_dependencies():
    record = RepositoryContext(record_id="id", scope=REPO, summary="safe")
    for scope in (
        OWNER,
        replace(REPO, owner_id="other"),
        Scope("owner1", replace(REPO.repository, repo_path="/tmp/other")),
    ):
        ref = RecordReference("repository", "target", 1, scope)
        with pytest.raises(ContextValidationError):
            sanitized_record(replace(record, references=(ref,)))
    for record in (
        OwnerContext(record_id="id", scope=REPO, summary="safe"),
        replace(record, scope=OWNER),
        replace(
            record,
            scope=Scope("owner1", replace(REPO.repository, repo_path="/tmp/../bad")),
        ),
        InitiativeContext(
            record_id="id", scope=OWNER, summary="safe", dependency_ids=("id", "id")
        ),
    ):
        with pytest.raises(ContextValidationError):
            sanitized_record(record)


def test_redaction_uses_config_and_rejects_sensitive_identity(monkeypatch):
    from ai_agent import context_validation

    monkeypatch.setattr(
        context_validation,
        "redact_sensitive",
        lambda text: text.replace("configured-secret", "[redacted]"),
    )
    record = OwnerContext(
        record_id="id",
        scope=OWNER,
        summary="configured-secret token=abc Bearer xyz password:foo",
    )
    safe = sanitized_record(record)
    assert (
        safe.summary
        == "[redacted] token=[redacted] Bearer [redacted] password:[redacted]"
    )
    with pytest.raises(ContextValidationError):
        sanitized_record(replace(record, record_id="configured-secret"))


def test_context_root_is_read_at_import(tmp_path, monkeypatch):
    import importlib.util

    from ai_agent import config

    monkeypatch.setenv("CONTEXT_ROOT", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "isolated_context_config", config.__file__
    )
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    assert isolated.CONTEXT_ROOT == tmp_path
    monkeypatch.setenv("CONTEXT_ROOT", "/tmp/changed")
    assert isolated.CONTEXT_ROOT == tmp_path
