"""The Coding snapshot's optional token_usage section (format 2)."""

import json
import tempfile
import unittest
from pathlib import Path

from ai_agent import token_usage
from ai_agent.bot.snapshot_publisher import (
    SNAPSHOT_FORMAT,
    SnapshotPublisher,
    build_content,
)
from ai_agent.token_usage import UsageRecord, record_usage, usage_section

PROJECT = {"name": "channel-cast", "repository": "ramunl/x", "branch": "main"}
IMAGE_USAGE = {"used_bytes": 0, "cap_bytes": 10, "count": 0}
VERSIONS = {"version": "ai-coding-agent v0.3.0", "core": "core: v1.1"}
TODAY = "2026-10-21"


def _user_data() -> dict:
    return {
        "task_queue": [{"id": 3, "branch_name": "feature/a", "codex_prompt": "SECRET"}],
        "active_execution": {
            "branch": "feature/b",
            "phase": "Polling CI",
            "status": "RUNNING",
        },
    }


class SnapshotUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.store = self.dir / "token-usage.json"

    def test_format_is_two_and_the_section_is_optional(self) -> None:
        self.assertEqual(SNAPSHOT_FORMAT, 2)
        content = build_content(
            _user_data(), PROJECT, VERSIONS, image_storage=IMAGE_USAGE
        )
        self.assertNotIn("token_usage", content)

    def test_existing_keys_are_still_published(self) -> None:
        content = build_content(
            _user_data(), PROJECT, VERSIONS, image_storage=IMAGE_USAGE
        )
        for key in ("format", "image_storage", "queue", "running", "project"):
            self.assertIn(key, content)
        self.assertEqual(content["format"], 2)

    def test_section_is_published_when_the_store_is_readable(self) -> None:
        record_usage(UsageRecord("planner", "codex", 12, TODAY), self.store)
        section = usage_section(self.store, today=TODAY)
        content = build_content(
            _user_data(),
            PROJECT,
            VERSIONS,
            image_storage=IMAGE_USAGE,
            token_usage=section,
        )
        self.assertEqual(content["token_usage"], section)

    def test_missing_store_still_publishes_without_the_section(self) -> None:
        with self.assertLogs(token_usage.logger.name, level="INFO"):
            section = usage_section(self.store, today=TODAY)
        content = build_content(
            _user_data(),
            PROJECT,
            VERSIONS,
            image_storage=IMAGE_USAGE,
            token_usage=section,
        )
        path = self.dir / "snapshot.json"
        self.assertTrue(SnapshotPublisher(path).publish(content, now=1000.0))
        written = json.loads(path.read_text())
        self.assertNotIn("token_usage", written)
        self.assertEqual(written["format"], 2)

    def test_published_section_holds_only_counts_dates_roles_and_tools(self) -> None:
        record_usage(UsageRecord("planner", "codex", 120, TODAY), self.store)
        record_usage(UsageRecord("implementer", "claude", None, TODAY), self.store)
        section = usage_section(self.store, today=TODAY)
        content = build_content(
            _user_data(),
            PROJECT,
            VERSIONS,
            image_storage=IMAGE_USAGE,
            token_usage=section,
        )

        text = json.dumps(content["token_usage"])
        for word in ("SECRET", "feature/", "prompt", "diff", "branch", "Polling"):
            self.assertNotIn(word, text)
        for day in content["token_usage"]["days"]:
            for row in day["usage"]:
                self.assertEqual(
                    set(row), {"role", "tool", "calls", "unknown", "tokens"}
                )
                self.assertIn(row["role"], {"planner", "implementer"})
                self.assertIn(row["tool"], {"codex", "claude"})
                for name in ("calls", "unknown", "tokens"):
                    self.assertIsInstance(row[name], int)


if __name__ == "__main__":
    unittest.main()
