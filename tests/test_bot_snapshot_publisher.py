"""The coding agent's published read model: the dashboard's only input."""

import asyncio
import json
import os
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_agent.bot import snapshot_publisher
from ai_agent.bot.snapshot_publisher import SnapshotPublisher, build_content

OWNER = 42
PROJECT = {"name": "channel-cast", "repository": "ramunl/x", "branch": "main"}
VERSIONS = {"version": "ai-coding-agent v0.3.0", "core": "core: v1.1"}


def _user_data() -> dict:
    return {
        "task_queue": [{"id": 3, "branch_name": "feature/a", "codex_prompt": "SECRET"}],
        "active_execution": {
            "branch": "feature/b",
            "phase": "Polling CI",
            "status": "RUNNING",
        },
    }


class ContentTests(unittest.TestCase):
    def test_includes_running_task_that_state_json_never_stores(self) -> None:
        content = build_content(_user_data(), PROJECT, VERSIONS)
        self.assertEqual(content["running"]["phase"], "Polling CI")
        self.assertEqual(content["queue"][0]["branch"], "feature/a")
        self.assertEqual(content["project"]["name"], "channel-cast")
        self.assertEqual(content["core"], "core: v1.1")

    def test_is_secret_free(self) -> None:
        self.assertNotIn(
            "SECRET", json.dumps(build_content(_user_data(), PROJECT, VERSIONS))
        )


class PublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "snapshot.json"
        self.publisher = SnapshotPublisher(self.path, heartbeat=30)
        self.content = build_content(_user_data(), PROJECT, VERSIONS)

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def test_first_publish_writes_owner_only_file_with_timestamp(self) -> None:
        self.assertTrue(self.publisher.publish(self.content, now=1000.0))
        self.assertEqual(self._read()["updated_at"], 1000.0)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_unchanged_content_is_not_rewritten_before_heartbeat(self) -> None:
        self.publisher.publish(self.content, now=1000.0)
        self.assertFalse(self.publisher.publish(dict(self.content), now=1010.0))
        self.assertEqual(self._read()["updated_at"], 1000.0)

    def test_heartbeat_refreshes_timestamp_while_idle(self) -> None:
        self.publisher.publish(self.content, now=1000.0)
        self.assertTrue(self.publisher.publish(dict(self.content), now=1031.0))
        self.assertEqual(self._read()["updated_at"], 1031.0)

    def test_change_is_written_immediately(self) -> None:
        self.publisher.publish(self.content, now=1000.0)
        changed = {**self.content, "running": None}
        self.assertTrue(self.publisher.publish(changed, now=1001.0))
        self.assertIsNone(self._read()["running"])

    def test_failed_write_is_retried_next_tick(self) -> None:
        with patch.object(snapshot_publisher, "write_json_atomic", return_value=False):
            self.assertFalse(self.publisher.publish(self.content, now=1000.0))
        self.assertTrue(self.publisher.publish(self.content, now=1001.0))


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_publishes_and_survives_a_failing_tick(self) -> None:
        path = Path(tempfile.mkdtemp()) / "snapshot.json"
        app = types.SimpleNamespace(user_data={OWNER: _user_data()})
        calls = {"n": 0}

        def flaky_project() -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("registry temporarily unreadable")
            return PROJECT

        with (
            patch.object(snapshot_publisher, "_versions", return_value=VERSIONS),
            patch.object(snapshot_publisher, "_project", side_effect=flaky_project),
        ):
            task = asyncio.create_task(
                snapshot_publisher.publish_forever(app, OWNER, path, interval=0.01)
            )
            for _ in range(200):
                if path.exists():
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertGreaterEqual(calls["n"], 2)  # first tick failed, loop kept going
        self.assertEqual(json.loads(path.read_text())["running"]["branch"], "feature/b")


if __name__ == "__main__":
    unittest.main()
