"""Owner capabilities for bounded inspection and revision-checked archive edits."""

import json
import re
import time
from dataclasses import dataclass, replace

from ai_agent.context_models import Approval, ContextRecord, RecordReference
from ai_agent.context_store import ContextStore
from ai_agent.context_validation import (
    ContextValidationError,
    ScopeMismatchError,
    _redact_text,
    opaque_id,
)

MAX_INSPECT_RECORDS = 4
MAX_INSPECT_TEXT = 300
MAX_RESPONSE_BYTES = 3500
MAX_INSPECT_ITEMS = 8


@dataclass(frozen=True)
class OwnerEvent:
    """Trusted adapter input, never decoded from a model or command arguments."""

    actor_id: str
    chat_id: str
    update_id: int


class ManagementError(ValueError):
    """A safe management refusal."""


def _text(value: str) -> tuple[str, bool]:
    safe = _redact_text(value)
    # Curated text can still contain local paths. Omit POSIX and Windows paths.
    safe = re.sub(r"(?:[A-Za-z]:\\|/|~/)[^\s\"'<>]+", "[path]", safe)
    safe = " ".join(safe.split())
    return safe[:MAX_INSPECT_TEXT], len(safe) > MAX_INSPECT_TEXT


def _view(record: ContextRecord) -> dict:
    summary, truncated = _text(record.summary)
    decisions = []
    for decision in record.decisions[:MAX_INSPECT_ITEMS]:
        text, cut = _text(decision.summary)
        truncated |= cut
        decisions.append(
            dict(id=decision.decision_id, summary=text, status=decision.status)
        )
    refs = [
        dict(kind=ref.kind, id=ref.record_id, revision=ref.revision)
        for ref in record.references[:MAX_INSPECT_ITEMS]
    ]
    return dict(
        kind=record.kind,
        id=record.record_id,
        revision=record.revision,
        summary=summary,
        decisions=decisions,
        references=refs,
        truncated=truncated
        or len(record.decisions) > MAX_INSPECT_ITEMS
        or len(record.references) > MAX_INSPECT_ITEMS,
    )


class ContextManagement:
    """Use a scoped store after authenticating an event against configured owner."""

    def __init__(self, store: ContextStore, owner_id: str, event: OwnerEvent) -> None:
        """Authenticate trusted adapter identity before any archive access."""
        if (
            not owner_id
            or event.actor_id != owner_id
            or event.chat_id != owner_id
            or store.scope.owner_id != owner_id
            or type(event.update_id) is not int
            or event.update_id < 0
        ):
            raise ManagementError("Owner authentication required")
        self.store = store
        self.owner_id = owner_id
        self.event = event

    def _read(self, ref: RecordReference) -> ContextRecord:
        if ref.scope != self.store.scope:
            raise ScopeMismatchError("Record outside selected scope")
        if type(ref.revision) is not int or not 1 <= ref.revision <= 1_000_000:
            raise ContextValidationError("A pinned revision is required")
        return self.store.get(ref.kind, ref.record_id, ref.revision)

    def inspect(self, refs: tuple[RecordReference, ...]) -> str:
        """Return bounded JSON without raw envelopes, evidence paths or provenance."""
        result = {"records": [], "truncated": len(refs) > MAX_INSPECT_RECORDS}
        for ref in refs[:MAX_INSPECT_RECORDS]:
            entry = _view(self._read(ref))
            result["records"].append(entry)
            if len(json.dumps(result).encode()) > MAX_RESPONSE_BYTES:
                result["records"].pop()
                result["truncated"] = True
                break
            result["truncated"] |= entry["truncated"]
        return json.dumps(result, ensure_ascii=True)

    def _mutable(self, ref: RecordReference) -> ContextRecord:
        record = self._read(ref)
        if record.kind == "attempt":
            raise ManagementError("Attempt snapshots are immutable")
        return record

    def link(self, source: RecordReference, target: RecordReference) -> ContextRecord:
        """Link an existing exact-scope revision using the source revision as CAS."""
        record = self._mutable(source)
        self._read(target)
        if target in record.references:
            raise ManagementError("Reference already linked")
        return self.store.write(
            replace(record, references=record.references + (target,)),
            expected_revision=source.revision,
        )

    def decide(
        self, source: RecordReference, decision_id: str, *, approve: bool
    ) -> ContextRecord:
        """Approve a proposal from trusted provenance or remove it in a new revision."""
        opaque_id(decision_id)
        record = self._mutable(source)
        decision = next(
            (item for item in record.decisions if item.decision_id == decision_id), None
        )
        if decision is None or decision.status != "proposed":
            raise ManagementError("An existing proposed decision is required")
        decisions = tuple(
            item for item in record.decisions if item.decision_id != decision_id
        )
        if approve:
            approval = Approval(
                self.owner_id,
                f"telegram-{self.event.update_id}",
                int(time.time()),
                "telegram",
            )
            decisions = tuple(
                replace(item, status="approved", approval=approval)
                if item.decision_id == decision_id
                else item
                for item in record.decisions
            )
        return self.store.write(
            replace(record, decisions=decisions), expected_revision=source.revision
        )
