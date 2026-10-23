"""Image usage collection must not block snapshot consumers or the event loop."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_agent.bot import snapshot_publisher as publisher

USAGE = {"used_bytes": 4, "cap_bytes": 10, "count": 1}


def test_build_content_uses_collected_usage_without_image_reads(monkeypatch):
    def unexpected_read():
        pytest.fail("Snapshot assembly read image storage")

    monkeypatch.setattr(publisher, "usage", unexpected_read)
    content = publisher.build_content({}, {}, {}, image_usage=USAGE)
    assert content["image_storage"] == USAGE
    assert content["format"] == 2


def test_slow_usage_leaves_event_loop_responsive(tmp_path, monkeypatch):
    started, resume = threading.Event(), threading.Event()
    released = []

    def slow_usage():
        started.set()
        released.append(resume.wait(3))
        return USAGE

    monkeypatch.setattr(publisher, "usage", slow_usage)
    for name in ("_versions", "_project", "files_part", "state_part"):
        monkeypatch.setattr(publisher, name, lambda *args: {})
    monkeypatch.setattr(publisher, "usage_section", lambda: None)
    monkeypatch.setattr(publisher, "sync_tasks", lambda data: False)
    monkeypatch.setattr(publisher, "poll_limits", AsyncMock())
    monkeypatch.setattr(publisher, "track_pulls", AsyncMock())
    monkeypatch.setattr(publisher.ModelChoices, "refresh_forever", AsyncMock())
    path = tmp_path / "snapshot.json"

    async def exercise():
        def debug():
            for pending in asyncio.all_tasks():
                pending.print_stack()
        asyncio.get_running_loop().call_later(7, debug)
        task = asyncio.create_task(
            publisher.publish_forever(SimpleNamespace(user_data={}), 42, path, 60)
        )
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            # This coroutine must run while usage is still waiting in its thread.
            assert not released, "Image usage blocked the event loop"
            assert not path.exists()
            resume.set()
            async with asyncio.timeout(3):
                while not path.exists():
                    await asyncio.sleep(0.01)
            assert json.loads(path.read_text())["image_storage"] == USAGE
            assert released == [True]
        finally:
            resume.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
