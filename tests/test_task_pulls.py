"""Tasks follow their pull request on GitHub: open, merged, closed."""

import asyncio
import importlib
import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TaskPullTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        self.pulls = importlib.import_module("ai_agent.task_pulls")
        self.tasks = importlib.import_module("ai_agent.tasks")
        self.data: dict = {}
        self.task = self.tasks.add_task(self.data, "add login", "repo", None)
        self.task.update(stage="pr", branch="feature/login")
        self.repo = patch.object(
            self.pulls,
            "get_project",
            lambda name: SimpleNamespace(github_repository="ramunl/repo"),
        )
        self.repo.start()

    def tearDown(self) -> None:
        self.repo.stop()
        os.environ.clear()
        os.environ.update(self.previous_env)

    def test_asks_github_for_the_branch_read_only(self) -> None:
        calls = []

        def fake(method, path, data=None, query=None):
            calls.append((method, path, query))
            return [
                {
                    "html_url": "https://github.com/ramunl/repo/pull/7",
                    "state": "closed",
                    "merged_at": "2026-10-08T10:00:00Z",
                    "merge_commit_sha": "abc123",
                }
            ]

        with patch.object(self.pulls, "github_request", fake):
            pull = self.pulls.pull_state("ramunl/repo", "feature/login")
        self.assertEqual(
            calls,
            [
                (
                    "GET",
                    "/repos/ramunl/repo/pulls",
                    {"head": "ramunl:feature/login", "state": "all", "per_page": "5"},
                )
            ],
        )
        self.assertEqual(pull["state"], "merged")
        self.assertEqual(pull["merge_commit"], "abc123")
        expected = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(pull["merged_at"], expected)

    def test_merged_closed_and_open_move_the_task(self) -> None:
        merged = {"url": "u", "state": "merged", "merged_at": 5.0, "merge_commit": "c"}
        self.assertTrue(self.pulls.apply_pull(self.task, merged))
        self.assertEqual((self.task["stage"], self.task["merged_at"]), ("done", 5.0))
        other = self.tasks.add_task(self.data, "x", "repo", None)
        other.update(stage="ended", note=self.tasks.ENDED_NOTE)
        self.assertTrue(self.pulls.apply_pull(other, {"url": "u2", "state": "open"}))
        self.assertEqual(
            (other["stage"], other["note"], other["pr_url"]), ("pr", None, "u2")
        )
        self.assertFalse(self.pulls.apply_pull(other, {"url": "u2", "state": "open"}))
        self.assertTrue(self.pulls.apply_pull(other, {"url": "u2", "state": "closed"}))
        self.assertEqual(other["stage"], "stopped")
        self.assertFalse(self.pulls.apply_pull(other, None))

    async def test_check_once_skips_other_stages_and_survives_failures(self) -> None:
        todo = self.tasks.add_task(self.data, "later", "repo", None)
        broken = self.tasks.add_task(self.data, "broken", "repo", None)
        broken.update(stage="pr", branch="feature/broken")

        def fake(repository, branch):
            if branch == "feature/broken":
                raise RuntimeError("GitHub API failed (404)")
            return {
                "url": "u",
                "state": "merged",
                "merged_at": 1.0,
                "merge_commit": "c",
            }

        with (
            patch.object(self.pulls, "pull_state", fake),
            self.assertLogs("ai_agent.task_pulls", "WARNING"),
        ):
            self.assertTrue(await self.pulls.check_once(self.data))
        self.assertEqual(self.task["stage"], "done")
        self.assertEqual(broken["stage"], "pr")
        self.assertEqual(todo["stage"], "todo")

    async def test_loop_marks_changes_for_saving(self) -> None:
        app = SimpleNamespace(
            user_data={123: self.data}, mark_data_for_update_persistence=MagicMock()
        )
        merged = {"url": "u", "state": "merged", "merged_at": 1.0, "merge_commit": "c"}
        with patch.object(self.pulls, "pull_state", lambda repository, branch: merged):
            loop = asyncio.create_task(
                self.pulls.track_forever(app, 123, interval=0.01)
            )
            await asyncio.sleep(0.05)
            loop.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop
        app.mark_data_for_update_persistence.assert_called_with(user_ids=123)
        self.assertIn("done", self.tasks.FINISHED)

    def test_an_ended_run_without_a_pull_request_is_stopped_not_pr_open(self) -> None:
        self.task.update(stage="ended", note=self.tasks.ENDED_NOTE)
        self.assertTrue(self.pulls.apply_pull(self.task, None))
        self.assertEqual(self.task["stage"], "stopped")
        self.assertIn("without a pull request", self.task["note"])
        pr_task = self.tasks.add_task(self.data, "y", "repo", None)
        pr_task.update(stage="pr", branch="b")
        self.assertFalse(self.pulls.apply_pull(pr_task, None))  # PR open stays

    async def test_ended_tasks_are_checked_again_soon(self) -> None:
        self.task.update(stage="ended")
        app = SimpleNamespace(
            user_data={123: self.data}, mark_data_for_update_persistence=MagicMock()
        )
        calls = []

        def first_none_then_open(repository, branch):
            calls.append(branch)
            return None if len(calls) == 1 else {"url": "u", "state": "open"}

        other = self.tasks.add_task(self.data, "z", "repo", None)
        other.update(stage="ended", branch="feature/z")
        with (
            patch.object(self.pulls, "pull_state", first_none_then_open),
            patch.object(self.pulls, "ENDED_POLL_SECONDS", 0.01),
        ):
            loop = asyncio.create_task(self.pulls.track_forever(app, 123, interval=60))
            await asyncio.sleep(0.05)
            loop.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop
        # Both settled on the first pass; no waiting for the 60-second interval.
        self.assertEqual(calls, ["feature/login", "feature/z"])
        self.assertEqual((self.task["stage"], other["stage"]), ("stopped", "pr"))
