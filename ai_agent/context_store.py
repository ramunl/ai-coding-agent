"""Authoritative append-only revisions with locked, durable filesystem writes."""

import fcntl
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Iterator

from ai_agent.config import CONTEXT_ROOT
from ai_agent.context_models import MAX_RECORD_BYTES, RECORD_TYPES, ContextRecord, Scope
from ai_agent.context_validation import (
    ContextValidationError,
    ScopeMismatchError,
    decode_record,
    opaque_id,
    references_of,
    sanitized_record,
    validate_scope,
    validate_transition,
)

MAX_SCAN_ENTRIES = 100_000
RECORD_NAME = re.compile(
    r"(owner|initiative|repository|attempt)\.([A-Za-z0-9_-]{1,64})\.([1-9][0-9]{0,6})\.json"
)


class ContextPersistenceError(RuntimeError):
    """Storage failed; after replacement, commit status may be uncertain."""


class RevisionConflictError(ContextPersistenceError):
    """Reload and reconcile instead of overwriting a concurrent revision."""


@contextmanager
def private_directory(path: Path) -> Iterator[int]:
    """Walk using directory descriptors, rejecting symlinks even during races."""
    if not path.is_absolute() or ".." in path.parts:
        raise ContextPersistenceError("Context path must be absolute without traversal")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        os.fchmod(descriptor, 0o700)
        yield descriptor
    except OSError:
        raise ContextPersistenceError("Context directory access failed") from None
    finally:
        os.close(descriptor)


def read_bytes(directory: int, name: str, limit: int = MAX_RECORD_BYTES) -> bytes:
    """Read a bounded regular file without following links or blocking on FIFOs."""
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContextPersistenceError("Context entry is not a private regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ContextValidationError("Record exceeds size limit")
    return data


def atomic_write(directory: int, name: str, payload: bytes) -> None:
    """Flush a private temporary file, replace atomically, then flush its directory."""
    temporary = f".pending-{secrets.token_hex(16)}"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def record_names(directory: int) -> Iterator[str]:
    """Bound directory scanning, including unrelated and crash-leftover entries."""
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries):
            if count >= MAX_SCAN_ENTRIES:
                raise ContextPersistenceError("Archive scan limit reached")
            if RECORD_NAME.fullmatch(entry.name):
                yield entry.name


class ContextStore:
    """A capability for exactly one owner/repository scope; no implicit selection."""

    def __init__(self, scope: Scope, root: Path = CONTEXT_ROOT) -> None:
        """Bind scope and root without accessing storage at import or construction."""
        validate_scope(scope)
        self.scope = scope
        self.root = Path(root)
        if (
            not self.root.is_absolute()
            or ".." in self.root.parts
            or self.root == Path("/")
        ):
            raise ContextPersistenceError("Unsafe context root")

    @contextmanager
    def directory(self) -> Iterator[int]:
        """Open the private owner directory with no symlink traversal."""
        with private_directory(self.root) as root_fd:
            try:
                os.mkdir(self.scope.owner_id, mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            try:
                descriptor = os.open(
                    self.scope.owner_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
                try:
                    os.fchmod(descriptor, 0o700)
                    yield descriptor
                finally:
                    os.close(descriptor)
            except OSError:
                raise ContextPersistenceError(
                    "Context storage operation failed"
                ) from None

    def _prefix(self, kind: str, record_id: str) -> str:
        if type(kind) is not str or kind not in RECORD_TYPES:
            raise ContextValidationError("Unknown record kind")
        return f"{kind}.{opaque_id(record_id)}."

    def _read(self, directory: int, name: str) -> ContextRecord:
        try:
            data = json.loads(read_bytes(directory, name))
        except (ValueError, UnicodeError, RecursionError):
            raise ContextValidationError("Unreadable context record") from None
        record = decode_record(data)
        expected = f"{record.kind}.{record.record_id}.{record.revision}.json"
        if name != expected:
            raise ContextValidationError("Record identity mismatch")
        if record.scope != self.scope:
            raise ScopeMismatchError("Record outside store scope")
        return record

    def get(
        self, kind: str, record_id: str, revision: int | None = None
    ) -> ContextRecord:
        """Read a pinned revision or the latest; never fall back past corrupt data."""
        prefix = self._prefix(kind, record_id)
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ContextValidationError("Invalid requested revision")
        with self.directory() as directory:
            if revision is None:
                revision = self._latest(directory, prefix)
                if revision == 0:
                    raise ContextPersistenceError("Context record missing")
            return self._read(directory, f"{prefix}{revision}.json")

    def _latest(self, directory: int, prefix: str) -> int:
        return max(
            (
                int(name.split(".")[-2])
                for name in record_names(directory)
                if name.startswith(prefix)
            ),
            default=0,
        )

    def write(self, record: ContextRecord, *, expected_revision: int) -> ContextRecord:
        """Commit one revision; attempts are write-once, references must exist."""
        safe = sanitized_record(record)
        if safe.scope != self.scope:
            raise ContextValidationError("Write outside store scope")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ContextValidationError("Invalid expected revision")
        if safe.revision != expected_revision:
            raise RevisionConflictError("Draft revision differs from expected revision")
        prefix = self._prefix(safe.kind, safe.record_id)
        with self.directory() as directory:
            lock = os.open(
                f"{prefix}lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=directory,
            )
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ContextPersistenceError("Invalid context lock")
                os.fchmod(lock, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                return self._commit(directory, prefix, safe, expected_revision)
            finally:
                os.close(lock)

    def _commit(
        self, directory: int, prefix: str, record: ContextRecord, expected: int
    ) -> ContextRecord:
        latest = self._latest(directory, prefix)
        if latest != expected or (record.kind == "attempt" and latest):
            raise RevisionConflictError("Stale revision or immutable attempt")
        if latest:
            previous = self._read(directory, f"{prefix}{latest}.json")
            validate_transition(previous, record)
        for ref in references_of(record):
            self._read(directory, f"{ref.kind}.{ref.record_id}.{ref.revision}.json")
        committed = replace(record, revision=latest + 1)
        committed = decode_record(asdict(committed))
        payload = json.dumps(asdict(committed), ensure_ascii=True).encode()
        atomic_write(directory, f"{prefix}{committed.revision}.json", payload)
        return committed
