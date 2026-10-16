"""Validated, private image storage with serialized oldest-first eviction."""

from __future__ import annotations

import contextlib
import fcntl
import io
import logging
import os
import re
import secrets
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # text-only operation remains available until dependencies install
    Image = None

from ai_agent.config import (
    IMAGE_DIR,
    IMAGE_MAX_BYTES,
    IMAGE_MAX_PER_TASK,
    IMAGE_STORAGE_CAP_BYTES,
)

logger = logging.getLogger(__name__)
MAX_PIXELS = 16_000_000
MAX_DIMENSION = 8000
_ID = re.compile(r"[0-9]{20}-[0-9a-f]{32}\.(png|jpeg|webp)\Z")
_LOCK = threading.RLock()


class ImageError(ValueError):
    """An upload cannot safely be accepted."""


def decode(data: bytes) -> tuple[str, int, int]:
    """Decode the entire single-frame image, ignoring declared names and MIME."""
    if Image is None:
        raise ImageError(
            "Image decoding dependency is unavailable; install requirements."
        )
    if not data or len(data) > IMAGE_MAX_BYTES:
        raise ImageError("Image is empty or exceeds the image byte limit.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                fmt = (image.format or "").lower()
                width, height = image.size
                if fmt not in {"png", "jpeg", "webp"}:
                    raise ImageError("Only PNG, JPEG and WebP images are supported.")
                if (
                    width * height > MAX_PIXELS
                    or max(width, height) > MAX_DIMENSION
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ImageError("Image dimensions or animation exceed limits.")
                image.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
            return fmt, width, height
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ImageError("Image could not be decoded safely.") from error


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """Coordinate threads and processes; the store is writable only by the bot."""
    with _LOCK:
        IMAGE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (IMAGE_DIR / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _files() -> list[Path]:
    return sorted(
        p
        for p in IMAGE_DIR.iterdir()
        if _ID.fullmatch(p.name) and p.is_file() and not p.is_symlink()
    )


def _evict(reserve: int = 0) -> None:
    files = _files()
    used = sum(p.stat().st_size for p in files)
    for path in files:
        if used + reserve <= IMAGE_STORAGE_CAP_BYTES:
            break
        size = path.stat().st_size
        path.unlink()
        used -= size
        logger.info("Evicted one task image (oldest first)")


def initialize(references: list[dict] | None = None) -> None:
    """Remove interrupted writes and enforce the configured cap at startup."""
    with locked():
        for path in IMAGE_DIR.glob("*.part"):
            path.unlink(missing_ok=True)
        if references is not None:
            retained = {ref.get("id") for ref in references}
            for path in _files():
                if path.name not in retained:
                    path.unlink()
        _evict()


def save(data: bytes) -> dict:
    """Atomically save validated bytes; no temporary disk copy escapes the cap."""
    fmt, width, height = decode(data)
    if len(data) > IMAGE_STORAGE_CAP_BYTES:
        raise ImageError("Image exceeds the configured storage cap.")
    try:
        with locked():
            files = _files()
            # Monotonic stored creation order survives backwards wall-clock jumps.
            created = max(time.time_ns(), int(files[-1].name[:20]) + 1 if files else 0)
            name = f"{created:020d}-{secrets.token_hex(16)}.{fmt}"
            path = IMAGE_DIR / name
            temporary = path.with_suffix(".part")
            _evict(len(data))
            try:
                with temporary.open("xb") as output:
                    os.chmod(temporary, 0o600)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "id": name,
            "bytes": len(data),
            "media_type": f"image/{fmt}",
            "width": width,
            "height": height,
            "created_at": created / 1e9,
        }
    except OSError as error:
        raise ImageError(
            "Image storage unavailable or full; upload not saved."
        ) from error


def remove(images: list[dict]) -> None:
    """Delete opaque references, ignoring already evicted files."""
    if not images:
        return
    with locked():
        for ref in images:
            name = ref.get("id", "")
            if isinstance(name, str) and _ID.fullmatch(name):
                (IMAGE_DIR / name).unlink(missing_ok=True)


def resolve(images: list[dict]) -> tuple[list[tuple[str, bytes]], int]:
    """Read a bounded batch under the eviction lock; bytes outlive eviction."""
    if not images:
        return [], 0
    found = []
    with locked():
        for ref in images[:IMAGE_MAX_PER_TASK]:
            name = ref.get("id", "")
            if not isinstance(name, str) or not _ID.fullmatch(name):
                continue
            try:
                fd = os.open(IMAGE_DIR / name, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as source:
                    data = source.read(IMAGE_MAX_BYTES + 1)
                fmt, _, _ = decode(data)
                found.append((f"image/{fmt}", data))
            except (OSError, ImageError):
                continue
    missing = len(images) - len(found)
    if missing:
        logger.warning("%d task image(s) evicted or unavailable", missing)
    return found, missing


def usage() -> dict[str, int]:
    """Return aggregate usage without identifiers, paths or image content."""
    if not IMAGE_DIR.exists():
        return {"used_bytes": 0, "cap_bytes": IMAGE_STORAGE_CAP_BYTES, "count": 0}
    with locked():
        files = _files()
        return {
            "used_bytes": sum(p.stat().st_size for p in files),
            "cap_bytes": IMAGE_STORAGE_CAP_BYTES,
            "count": len(files),
        }


def references_in(data: dict) -> list[dict]:
    """Collect live references at startup, including restored pending work."""
    records = [
        *(data.get("tasks") or []),
        *(data.get("task_queue") or []),
        data.get("pending_implementation") or {},
    ]
    plan = data.get("pending_plan")
    refs = list(getattr(plan, "images", []))
    for record in records:
        refs.extend(record.get("images") or [])
    return refs
