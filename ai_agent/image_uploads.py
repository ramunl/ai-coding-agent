"""One-time inbox upload claims, bounded reads and staging cleanup."""

from __future__ import annotations

import os
import re
import shutil
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

from ai_agent.config import IMAGE_MAX_BYTES, IMAGE_MAX_PER_TASK
from ai_agent.task_images import ImageError, remove, save

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REQUEST = re.compile(r"[0-9a-f]{8,32}\Z")
TTL = 120


def validate_names(images: object) -> list[str]:
    """Validate the public upload schema without interpreting any path."""
    if not isinstance(images, list) or not 1 <= len(images) <= IMAGE_MAX_PER_TASK:
        raise ImageError("images must contain between 1 and the task limit entries.")
    names = []
    for item in images:
        if not isinstance(item, dict) or set(item) != {"name"}:
            raise ImageError("Each image must have exactly a name.")
        name = item["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or ".." in name:
            raise ImageError("Invalid image upload name.")
        names.append(name)
    if len(set(names)) != len(names):
        raise ImageError("Duplicate image upload name.")
    return names


def _directory(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def mark_request(inbox: Path, request_id: str) -> None:
    """Reserve a request ID durably, without following a replaced ledger."""
    ledger = inbox / ".image-claims"
    ledger.mkdir(mode=0o700, exist_ok=True)
    directory = _directory(ledger)
    try:
        fd = os.open(
            request_id,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        os.close(fd)
        os.fsync(directory)
    finally:
        os.close(directory)


def claim(inbox: Path, request_id: str, images: object) -> list[dict]:
    """Consume a staging directory once using a durable exclusive claim marker."""
    names = validate_names(images)
    refs = []
    try:
        with _claim_directory(inbox, request_id) as directory:
            with os.scandir(directory) as entries_iter:
                entries = [
                    entry.name
                    for entry in islice(entries_iter, IMAGE_MAX_PER_TASK + 2)
                    if entry.name != ".claimed"
                ]
            if set(entries) != set(names):
                raise ImageError("Upload is incomplete or contains unexpected files.")
            for name in names:
                fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or not 0 < info.st_size <= IMAGE_MAX_BYTES
                    ):
                        raise ImageError("Upload is not a bounded regular file.")
                    data = source.read(IMAGE_MAX_BYTES + 1)
                    if len(data) != info.st_size:
                        raise ImageError("Upload changed while being read.")
                refs.append(save(data))
        return refs
    except (OSError, ImageError) as error:
        remove(refs)
        raise ImageError(
            "Upload missing, replayed, unsafe, incomplete or storage full."
        ) from error
    finally:
        cleanup(inbox, request_id)


# Keep descriptor-based traversal separate from validation and decoding.


@contextmanager
def _claim_directory(inbox: Path, request_id: str) -> Iterator[int]:
    if not _REQUEST.fullmatch(request_id):
        raise ImageError("Invalid upload request id.")
    root = _directory(inbox / "uploads")
    try:
        # Marker lives until the original directory is cleaned. Re-uploading the
        # same id is also refused by the inbox's request replay ledger.
        directory = os.open(
            request_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
        )
        try:
            marker = os.open(
                ".claimed",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory,
            )
            os.close(marker)
            # listdir consumer expects only supplied images.
            yield directory
        finally:
            os.close(directory)
    finally:
        os.close(root)


def cleanup(inbox: Path, request_id: str) -> None:
    """Remove a failed/consumed staging tree without following symlinks."""
    if not _REQUEST.fullmatch(request_id):
        return
    try:
        root = _directory(inbox / "uploads")
    except FileNotFoundError:
        return
    except OSError:
        return  # never follow a replaced staging root
    try:
        info = os.stat(request_id, dir_fd=root, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(request_id, dir_fd=root)
        else:
            os.unlink(request_id, dir_fd=root)
    except FileNotFoundError:
        pass
    finally:
        os.close(root)


def expire(inbox: Path, now: float | None = None) -> None:
    """Sweep abandoned staging directories even when no request was published."""
    root = inbox / "uploads"
    if not root.exists() or root.is_symlink():
        return
    current = time.time() if now is None else now
    paths = sorted(root.iterdir(), key=lambda p: p.lstat().st_mtime)
    # Dashboard must enforce this same 4-batch budget while streaming uploads.
    # The bot also removes abandoned/excess batches on every poll.
    for index, path in enumerate(paths):
        if _REQUEST.fullmatch(path.name) and (
            current - path.lstat().st_mtime > TTL or index < len(paths) - 4
        ):
            cleanup(inbox, path.name)
