"""Publish the coding agent's read model to a file for the dashboard service.

The dashboard runs as its own service so it keeps working when an agent
crashes. It therefore never talks to this process; it reads the file written
here. The file is the contract between the two.

What it contains is snapshot() (secret-free: no prompts, diffs, or logs) plus
the active project and versions. It is written when the content changes, and
at least every HEARTBEAT_SECONDS, so the dashboard can tell "idle" apart from
"the coding agent stopped publishing".
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from ai_agent.bot.atomic_file import write_json_atomic
from ai_agent.bot.state import snapshot

logger = logging.getLogger(__name__)

SNAPSHOT_FORMAT = 1
INTERVAL_SECONDS = 3.0
HEARTBEAT_SECONDS = 30.0


def _versions() -> dict:
    """Agent and core version. Only a deploy changes them, and a deploy restarts us."""
    from ai_agent.bot.maintenance import core_version_line
    from ai_agent.version import get_runtime_version

    return {"version": get_runtime_version(), "core": core_version_line()}


def _project() -> dict:
    """Active project; re-read every tick because /repo_use can switch it."""
    from ai_agent.projects import active_project

    project = active_project()
    return {
        "name": project.name,
        "repository": project.github_repository,
        "branch": project.base_branch,
    }


def build_content(user_data: dict, project: dict, versions: dict) -> dict:
    """Everything the Coding window shows, minus the timestamps."""
    return {
        "format": SNAPSHOT_FORMAT,
        **snapshot(user_data),
        "project": project,
        **versions,
    }


class SnapshotPublisher:
    """Writes the snapshot on change and on heartbeat; never raises."""

    def __init__(self, path: Path, heartbeat: float = HEARTBEAT_SECONDS) -> None:
        self.path = Path(path)
        self.heartbeat = heartbeat
        self._last_content: dict | None = None
        self._last_written = 0.0

    def publish(self, content: dict, now: float | None = None) -> bool:
        """Write if content changed or the heartbeat is due. Returns True if written."""
        current = time.time() if now is None else now
        changed = content != self._last_content
        due = current - self._last_written >= self.heartbeat
        if not (changed or due):
            return False
        payload = {**content, "updated_at": current, "pid": os.getpid()}
        if write_json_atomic(self.path, payload):
            self._last_content = content
            self._last_written = current
            return True
        return False


async def publish_forever(
    ptb_app: Any,
    owner_id: int,
    path: Path,
    interval: float = INTERVAL_SECONDS,
) -> None:
    """Run until cancelled. A failing tick is logged and retried on the next one."""
    publisher = SnapshotPublisher(path)
    try:
        versions = await asyncio.to_thread(_versions)
    except Exception as error:  # versions are nice-to-have
        logger.warning("Snapshot: could not read versions: %s", error)
        versions = {"version": "unknown", "core": "unknown"}
    while True:
        try:
            project = await asyncio.to_thread(_project)
            # Read queue state on the event-loop thread, where handlers mutate
            # it, so the published view is consistent.
            content = build_content(
                ptb_app.user_data.get(owner_id, {}), project, versions
            )
            publisher.publish(content)
        except Exception as error:
            logger.warning("Snapshot publish failed (will retry): %s", error)
        await asyncio.sleep(interval)
