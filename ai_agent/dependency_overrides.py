"""Explicit owner-reviewed dependency exceptions, stored as immutable decisions.

No model, dashboard or worker action calls this API. A trusted owner adapter must
supply Approval provenance and retain the returned reference on the dependency.
"""

import hashlib
import json
from dataclasses import asdict, replace
from uuid import uuid4

from ai_agent.context_models import (
    Approval,
    Decision,
    Dependency,
    InitiativeContext,
    RecordReference,
)
from ai_agent.context_store import ContextStore


def binding(dependency: Dependency) -> str:
    """Bind policy, predecessor, attempt, scope and dependency revision exactly."""
    return hashlib.sha256(
        json.dumps(asdict(replace(dependency, override=None)), sort_keys=True).encode()
    ).hexdigest()


def approve_override(
    dependency: Dependency, approval: Approval, reason: str, store: ContextStore
) -> RecordReference:
    """Persist an owner-reviewed exception before returning its audit reference."""
    if (
        approval.owner_id != dependency.scope.owner_id
        or store.scope != dependency.scope
        or not reason.strip()
    ):
        raise ValueError("Scoped owner approval and reason required")
    record = InitiativeContext(
        record_id=uuid4().hex,
        scope=dependency.scope,
        summary=reason,
        decisions=(Decision(binding(dependency), reason, "approved", approval),),
        dependencies=(replace(dependency, override=None),),
    )
    saved = store.write(record, expected_revision=0)
    return RecordReference("initiative", saved.record_id, saved.revision, saved.scope)


def verify_override(dependency: Dependency, store: ContextStore | None = None) -> dict:
    """Only an archived approved decision for this exact edge bypasses evidence."""
    ref = dependency.override
    store = store or ContextStore(dependency.scope)
    if store.scope != dependency.scope:
        raise ValueError("Override archive scope mismatch")
    record = store.get("initiative", ref.record_id, ref.revision)
    expected = replace(dependency, override=None)
    if record.dependencies != (expected,) or not any(
        d.decision_id == binding(dependency)
        and d.status == "approved"
        and d.approval
        and d.approval.owner_id == dependency.scope.owner_id
        for d in record.decisions
    ):
        raise ValueError("Dependency override is stale or unaudited")
    return {
        "dependency": asdict(dependency),
        "override": asdict(ref),
        "reason": record.summary,
    }
