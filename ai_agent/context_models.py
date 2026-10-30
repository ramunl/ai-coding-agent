"""Provider-independent, version-one archive contracts (no execution payloads)."""

from dataclasses import dataclass
from typing import Literal

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024
MAX_ITEMS = 64
MAX_TEXT = 4000
Kind = Literal["owner", "initiative", "repository", "attempt"]


@dataclass(frozen=True)
class RepositoryIdentity:
    """Use the same identity fields as plan_state.capture_repository_identity."""

    project_name: str
    repo_path: str
    github_repository: str


@dataclass(frozen=True)
class Scope:
    """Explicit owner and optional repository boundary; never the active project."""

    owner_id: str
    repository: RepositoryIdentity | None = None


@dataclass(frozen=True)
class RecordReference:
    """Pin an archived record revision, independent of task-list positions."""

    kind: Kind
    record_id: str
    revision: int
    scope: Scope


@dataclass(frozen=True)
class EvidenceReference:
    """Small provenance pointer and summary, not the source payload."""

    evidence_id: str
    source: Literal["commit", "pull_request", "test", "document", "owner"]
    locator: str
    summary: str


@dataclass(frozen=True)
class PacketReference:
    """Identify a context packet by stable ID and digest, without its prompt."""

    packet_id: str
    sha256: str
    records: tuple[RecordReference, ...] = ()


@dataclass(frozen=True)
class Approval:
    """Owner approval provenance supplied by a trusted caller, not a model."""

    owner_id: str
    event_id: str
    approved_at: int
    channel: Literal["telegram", "dashboard", "manual"]


@dataclass(frozen=True)
class Decision:
    """A proposal is not authority until owner approval is recorded."""

    decision_id: str
    summary: str
    status: Literal["proposed", "approved", "superseded"] = "proposed"
    approval: Approval | None = None
    superseded_by: str | None = None


@dataclass(frozen=True, kw_only=True)
class ContextRecord:
    """Shared versioned envelope; revision zero denotes an unwritten draft."""

    record_id: str
    scope: Scope
    summary: str
    schema_version: int = SCHEMA_VERSION
    revision: int = 0
    decisions: tuple[Decision, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    references: tuple[RecordReference, ...] = ()


@dataclass(frozen=True, kw_only=True)
class OwnerContext(ContextRecord):
    """Owner-wide curated context."""

    kind: Literal["owner"] = "owner"


@dataclass(frozen=True, kw_only=True)
class InitiativeContext(ContextRecord):
    """A scoped initiative with explicit stable dependency IDs."""

    kind: Literal["initiative"] = "initiative"
    dependency_ids: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class RepositoryContext(ContextRecord):
    """Curated context for one captured repository identity."""

    kind: Literal["repository"] = "repository"


@dataclass(frozen=True, kw_only=True)
class AttemptSnapshot(ContextRecord):
    """Immutable task-attempt result; never stores prompts or transcripts."""

    kind: Literal["attempt"] = "attempt"
    task_id: str
    attempt_id: str
    outcome: Literal["succeeded", "failed", "blocked", "cancelled"]
    dependency_ids: tuple[str, ...] = ()
    packets: tuple[PacketReference, ...] = ()


RECORD_TYPES = {
    "owner": OwnerContext,
    "initiative": InitiativeContext,
    "repository": RepositoryContext,
    "attempt": AttemptSnapshot,
}
