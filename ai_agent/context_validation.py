"""Strict, bounded contract decoding and decision/scope validation."""

import json
import re
import types
from dataclasses import asdict, fields, is_dataclass
from pathlib import PurePosixPath
from typing import Literal, get_args, get_origin, get_type_hints

from ai_agent.config import redact_sensitive
from ai_agent.context_models import (
    MAX_ITEMS,
    MAX_RECORD_BYTES,
    MAX_TEXT,
    RECORD_TYPES,
    ContextRecord,
    Scope,
)


class ContextValidationError(ValueError):
    """A contract is invalid; messages never include submitted content."""


class ScopeMismatchError(ContextValidationError):
    """A valid record belongs to a different scope."""


class UnsupportedSchemaError(ContextValidationError):
    """Preserve this record for a reader supporting its schema."""


def opaque_id(value: str) -> str:
    """Validate a stable opaque identifier, never a path or display title."""
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ContextValidationError("Invalid opaque ID")
    return value


def _decode(value: object, annotation: object) -> object:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is types.UnionType:
        for option in args:
            try:
                return _decode(value, option)
            except ContextValidationError:
                pass
    elif origin is Literal:
        if value in args and type(value) is type(args[0]):
            return value
    elif origin is tuple:
        if isinstance(value, (list, tuple)) and len(value) <= MAX_ITEMS:
            return tuple(_decode(item, args[0]) for item in value)
    elif is_dataclass(annotation):
        if type(value) is dict:
            hints = get_type_hints(annotation)
            if set(value) <= set(hints):
                try:
                    return annotation(
                        **{
                            key: _decode(item, hints[key])
                            for key, item in value.items()
                        }
                    )
                except TypeError:
                    pass
    elif type(value) is annotation:
        if annotation is str and len(value) > MAX_TEXT:
            raise ContextValidationError("Text exceeds limit")
        return value
    raise ContextValidationError("Invalid field type or unknown field")


def _redact_text(value: str) -> str:
    return re.sub(
        r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+",
        r"\1[redacted]",
        redact_sensitive(value),
    )


def validate_scope(scope: Scope) -> None:
    """Check captured identity without accessing the repository or registry."""
    if type(scope) is not Scope:
        raise ContextValidationError("Invalid scope type")
    scope = _decode(asdict(scope), Scope)
    opaque_id(scope.owner_id)
    values = [scope.owner_id]
    if scope.repository is not None:
        values.extend(asdict(scope.repository).values())
    if any(_redact_text(value) != value for value in values):
        raise ContextValidationError("Sensitive scope identity")
    repo = scope.repository
    if repo is None:
        return
    if not repo.project_name or len(repo.project_name) > 128:
        raise ContextValidationError("Invalid project name")
    path = PurePosixPath(repo.repo_path)
    if not path.is_absolute() or ".." in path.parts:
        raise ContextValidationError("Invalid repository path")
    if not re.fullmatch(
        r"[A-Za-z0-9_-][A-Za-z0-9._-]*/[A-Za-z0-9_-][A-Za-z0-9._-]*",
        repo.github_repository,
    ):
        raise ContextValidationError("Invalid repository identity")


def _validate_decisions(record: ContextRecord) -> None:
    decisions = {item.decision_id: item for item in record.decisions}
    if len(decisions) != len(record.decisions):
        raise ContextValidationError("Duplicate decision ID")
    for item in record.decisions:
        opaque_id(item.decision_id)
        if not item.summary.strip():
            raise ContextValidationError("Empty decision")
        if item.status == "proposed":
            if item.approval is not None or item.superseded_by is not None:
                raise ContextValidationError("Proposal cannot claim approval")
            continue
        approval = item.approval
        if approval is None or approval.owner_id != record.scope.owner_id:
            raise ContextValidationError("Owner approval required")
        opaque_id(approval.event_id)
        if approval.approved_at <= 0:
            raise ContextValidationError("Invalid approval timestamp")
        if item.status == "approved" and item.superseded_by is not None:
            raise ContextValidationError("Approved decision has supersession link")
        if item.status == "superseded":
            target = decisions.get(item.superseded_by)
            if target is None or target.status == "proposed":
                raise ContextValidationError("Supersession requires approved target")
            visited = {item.decision_id}
            while target is not None:
                if target.decision_id in visited:
                    raise ContextValidationError("Cyclic supersession")
                visited.add(target.decision_id)
                target = decisions.get(target.superseded_by)


def references_of(record: ContextRecord) -> tuple:
    """Include packet references in scope and persistence checks."""
    return record.references + tuple(
        ref for packet in getattr(record, "packets", ()) for ref in packet.records
    )


def decode_record(data: dict) -> ContextRecord:
    """Reject unknown fields, unsupported schemas and malformed contracts."""
    if type(data) is not dict:
        raise ContextValidationError("Expected record object")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise UnsupportedSchemaError("Unsupported context schema")
    cls = RECORD_TYPES.get(data.get("kind")) if type(data.get("kind")) is str else None
    if cls is None:
        raise ContextValidationError("Unknown record kind")
    record = _decode(data, cls)
    opaque_id(record.record_id)
    validate_scope(record.scope)
    if not 0 <= record.revision <= 1_000_000:
        raise ContextValidationError("Invalid revision")
    if (record.kind == "owner" and record.scope.repository is not None) or (
        record.kind in {"repository", "attempt"} and record.scope.repository is None
    ):
        raise ContextValidationError("Wrong repository scope")
    for ref in references_of(record):
        opaque_id(ref.record_id)
        validate_scope(ref.scope)
        if ref.revision < 1 or ref.scope != record.scope:
            raise ContextValidationError("Reference outside scope or unversioned")
    for name in ("task_id", "attempt_id"):
        if hasattr(record, name):
            opaque_id(getattr(record, name))
    dependencies = getattr(record, "dependency_ids", ())
    for dependency in dependencies:
        opaque_id(dependency)
    if len(set(dependencies)) != len(dependencies):
        raise ContextValidationError("Duplicate dependency ID")
    for evidence in record.evidence:
        opaque_id(evidence.evidence_id)
    for packet in getattr(record, "packets", ()):
        opaque_id(packet.packet_id)
        if not re.fullmatch(r"[a-f0-9]{64}", packet.sha256):
            raise ContextValidationError("Invalid packet digest")
    _validate_decisions(record)
    if len(json.dumps(asdict(record)).encode()) > MAX_RECORD_BYTES:
        raise ContextValidationError("Record exceeds size limit")
    return record


def sanitized_record(record: ContextRecord) -> ContextRecord:
    """Redact configured and common credential forms before persistence."""

    def clean(value: object) -> object:
        if isinstance(value, str):
            return _redact_text(value)
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [clean(item) for item in value]
        return value

    if type(record) not in RECORD_TYPES.values():
        raise ContextValidationError("Unknown contract")
    validated = decode_record(asdict(record))
    safe = decode_record(clean(asdict(validated)))
    # Identity/provenance must never silently change due to redaction.
    for field in fields(record):
        if field.name not in {"summary", "decisions", "evidence"}:
            if getattr(safe, field.name) != getattr(record, field.name):
                raise ContextValidationError("Sensitive structural field")
    for before, after in zip(record.decisions, safe.decisions):
        if (before.decision_id, before.approval, before.superseded_by) != (
            after.decision_id,
            after.approval,
            after.superseded_by,
        ):
            raise ContextValidationError("Sensitive approval identity")
    return safe


def validate_transition(previous: ContextRecord, current: ContextRecord) -> None:
    """Approved text and provenance remain immutable across revisions."""
    current_decisions = {item.decision_id: item for item in current.decisions}
    for old in previous.decisions:
        new = current_decisions.get(old.decision_id)
        if old.status == "proposed":
            continue
        if new is None or new.summary != old.summary or new.approval != old.approval:
            raise ContextValidationError("Cannot rewrite approved decision")
        if new.status == "proposed" or (old.status == "superseded" and new != old):
            raise ContextValidationError("Cannot reverse decision state")
