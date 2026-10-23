"""Image usage collection must leave the snapshot event loop responsive."""

import asyncio
import contextlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ai_agent.bot import snapshot_publisher as publisher


def test_build_content_uses_collected_counts(monkeypatch):
    def unexpected_scan():
        raise AssertionError("build_content must not scan storage")

    monkeypatch.setattr(publisher, "usage", unexpected_scan)
    counts = {"used_bytes": 7, "cap_bytes": 10, "count": 2}
    content = publisher.build_content({}, {}, {}, image_storage=counts)
    assert content["image_storage"] == counts
    assert content["format"] == 2


def test_image_usage_keeps_event_loop_responsive(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    counts = {"used_bytes": 7, "cap_bytes": 10, "count": 2}
    loop_thread = threading.get_ident()
    scan_threads = []

    def slow_usage():
        scan_threads.append(threading.get_ident())
        started.set()
        if not release.wait(3):
            raise AssertionError("event loop did not release usage scan")
        return counts

    monkeypatch.setattr(publisher, "usage", slow_usage)
    for name in ("_versions", "_project", "files_part"):
        monkeypatch.setattr(publisher, name, lambda *args: {})
    monkeypatch.setattr(publisher, "usage_section", lambda: None)
    monkeypatch.setattr(publisher, "sync_tasks", lambda data: False)
    monkeypatch.setattr(publisher, "state_part", lambda data: {})
    for name in ("poll_limits", "track_pulls"):
        monkeypatch.setattr(publisher, name, AsyncMock())
    monkeypatch.setattr(publisher.ModelChoices, "refresh_forever", AsyncMock())
    path = tmp_path / "snapshot.json"

    async def run():
        task = asyncio.create_task(
            publisher.publish_forever(SimpleNamespace(user_data={}), 42, path, 0.01)
        )
        try:
            async with asyncio.timeout(2):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                assert not path.exists()
                assert scan_threads[0] != loop_thread
                release.set()
                while not path.exists():
                    await asyncio.sleep(0.001)
            assert json.loads(path.read_text())["image_storage"] == counts
        finally:
            release.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())
