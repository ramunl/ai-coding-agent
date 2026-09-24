"""Crash-safe JSON file writes, with no Telegram dependency.

Used for the bot's state file and for the dashboard snapshot. A reader sees
either the previous file or the new one, never a half-written file.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def write_json_atomic(path: Path, payload: dict) -> bool:
    """Write JSON via temp file + rename, owner-only; log and return False on error.

    A reader (the bot on restart, or the dashboard service) therefore sees
    either the previous file or the new one, never a half-written file.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError as error:
        logger.error("Could not write %s: %s", path, error)
        return False
    return True
