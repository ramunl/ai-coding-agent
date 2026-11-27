"""Inspect resolver edits before the controller stages exact conflict paths."""

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ai_agent.merge_conflict_stages import Conflict

MAX_CONFLICT_BYTES = 8 * 1024 * 1024
CONFLICT_MARKER = re.compile(rb"^(?:<<<<<<<|>>>>>>>|=======\r?$)", re.MULTILINE)


@dataclass(frozen=True)
class FileState:
    """A bounded fingerprint, without retaining file contents in provider context."""

    mode: int
    digest: bytes
    markers: bool = False


def state(checkout: Path, path: str) -> FileState | None:
    """Read a conflict path without following a replaced parent or file symlink."""
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"Unsafe conflict path: {path!r}")
    target = checkout / path
    parent = target.parent
    while parent != checkout:
        if parent.is_symlink():
            raise ValueError(f"Conflict path has a symlink parent: {path!r}")
        parent = parent.parent
    try:
        info = target.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        content = os.fsencode(os.readlink(target))
    elif stat.S_ISREG(info.st_mode):
        with os.fdopen(os.open(target, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            content = stream.read(MAX_CONFLICT_BYTES + 1)
        if len(content) > MAX_CONFLICT_BYTES:
            raise ValueError(f"Conflict file exceeds the staging limit: {path!r}")
    else:
        # Preserve already-supported staged gitlink resolutions, but never
        # automatically stage a directory or special file as a resolution.
        return FileState(info.st_mode, b"")
    return FileState(
        info.st_mode,
        hashlib.sha256(content).digest(),
        bool(CONFLICT_MARKER.search(content)),
    )


def changed_paths(
    checkout: Path, conflicts: list[Conflict], before: dict[str, FileState | None]
) -> list[str]:
    """Require actual marker-free edits or deletions before controller staging."""
    paths = []
    unresolved = []
    for conflict in conflicts:
        path = conflict.path
        if path not in before:
            raise ValueError(f"Resolver introduced a new conflict path: {path!r}")
        after = state(checkout, path)
        reason = None
        if after is not None and after.markers:
            reason = "conflict markers remain"
        elif after == before[path]:
            reason = "no resolution edits"
        elif after is not None and not (
            stat.S_ISREG(after.mode) or stat.S_ISLNK(after.mode)
        ):
            reason = "unsupported file type"
        if reason:
            unresolved.append(f"{path!r} ({reason})")
        else:
            paths.append(path)
    if unresolved:
        raise ValueError("Resolver left unresolved paths: " + "; ".join(unresolved))
    return paths
