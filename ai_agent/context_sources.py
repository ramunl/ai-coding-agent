"""Explicit authorized archive inputs and bounded, link-safe repository reads."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ai_agent.context_models import ContextRecord, RecordReference, Scope
from ai_agent.context_store import ContextStore, read_bytes
from ai_agent.context_validation import (
    _redact_text,
    opaque_id,
    sanitized_record,
    validate_scope,
)


class ContextBlockedError(RuntimeError):
    """Required context cannot be safely supplied; no provider should be invoked."""


@dataclass(frozen=True)
class RepositoryEvidence:
    """Mandatory repository file pinned by its complete UTF-8 content digest."""

    path: str
    sha256: str


@dataclass(frozen=True)
class ContextRequest:
    """Trusted caller inputs; authorization must come from an owner handler.

    Candidates are an explicit bounded discovery result, never an index claim of
    completeness. Constraints and prerequisite refs must be supplied separately.
    Reuse this request for retries/repairs; new attempts get a new attempt ID.
    """

    scope: Scope
    authorized_owner_id: str
    initiative_id: str
    task_id: str
    attempt_id: str
    stores: tuple[ContextStore, ...]
    constraints: tuple[RecordReference, ...] = ()
    prerequisites: tuple[RecordReference, ...] = ()
    candidates: tuple[RecordReference, ...] = ()
    dependency_ids: tuple[str, ...] = ()
    documents: tuple[RepositoryEvidence, ...] = ()
    query: str = ""
    reopen_history: tuple[dict, ...] = ()

    def validate(self) -> None:
        """Authorize all scopes before the first historical read."""
        validate_scope(self.scope)
        if self.scope.repository is None:
            raise ContextBlockedError(
                "Context requires an explicit repository identity"
            )
        for value in (self.initiative_id, self.task_id, self.attempt_id):
            opaque_id(value)
        if self.authorized_owner_id != self.scope.owner_id:
            raise ContextBlockedError("Context owner authorization required")
        refs = self.constraints + self.prerequisites + self.candidates
        if len(refs) > 64 or len(self.dependency_ids) > 64:
            raise ContextBlockedError("Context input limit exceeded; narrow sources")
        if any(type(entry) is not dict for entry in self.reopen_history):
            raise ContextBlockedError("Invalid reopen metadata")
        if len(self.reopen_history) > 64:
            raise ContextBlockedError("Reopen history exceeds context input limit")
        if len(self.documents) > 16 or len(self.query) > 4000:
            raise ContextBlockedError("Context document/query input limit exceeded")
        for document in self.documents:
            if _redact_text(document.path) != document.path:
                raise ContextBlockedError("Sensitive repository evidence path")
            if not re.fullmatch(r"[a-f0-9]{64}", document.sha256):
                raise ContextBlockedError(
                    "Repository evidence requires a SHA-256 digest"
                )
        for dependency in self.dependency_ids:
            opaque_id(dependency)
        for scope in [
            *(store.scope for store in self.stores),
            *(r.scope for r in refs),
        ]:
            validate_scope(scope)
            if scope.owner_id != self.authorized_owner_id or scope.repository not in (
                None,
                self.scope.repository,
            ):
                raise ContextBlockedError("Context source outside authorized scope")
        for ref in refs:
            opaque_id(ref.record_id)
            if ref.kind not in {"owner", "repository", "initiative", "attempt"} or (
                type(ref.revision) is not int or not 1 <= ref.revision <= 1_000_000
            ):
                raise ContextBlockedError("Invalid context reference")

    def read(self, ref: RecordReference) -> ContextRecord:
        """Read sanitized pinned evidence and check authoritative freshness."""
        self.validate()
        if ref not in self.constraints + self.prerequisites + self.candidates:
            raise ContextBlockedError("Context reference was not authorized")
        stores = [store for store in self.stores if store.scope == ref.scope]
        if len(stores) != 1:
            raise ContextBlockedError("Provide exactly one authorized store per scope")
        store = stores[0]
        record = sanitized_record(store.get(ref.kind, ref.record_id, ref.revision))
        latest = store.get(ref.kind, ref.record_id)
        if latest.revision != ref.revision:
            raise ContextBlockedError(
                f"Stale context {ref.kind}/{ref.record_id}; reconcile latest revision"
            )
        return record


def repository_read(root: Path, relative: str, limit: int) -> str:
    """Read a complete bounded file beneath root without following any symlink.

    Descriptor walks prevent substitution races; no directories are created or
    permissions changed. Oversize required evidence fails instead of truncating.
    """
    root = Path(root)
    path = Path(relative)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or path.is_absolute()
        or (".." in path.parts or not path.parts)
    ):
        raise ContextBlockedError("Repository evidence path escapes its root")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (*root.parts[1:], *path.parts[:-1]):
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return read_bytes(descriptor, path.name, limit).decode("utf-8")
    finally:
        os.close(descriptor)
