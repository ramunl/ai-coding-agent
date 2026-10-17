"""Recurring errors in the agents' logs become proposed tasks."""

import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

OWNER = 123
NOW = 1_800_000_000.0
LINES = {
    "ai-pm-agent": [
        "2026-10-12T10:00:01+0300 vps python[812]: TimeoutError: sync of todos took 31.2 s",
        "2026-10-12T11:30:44+0300 vps python[812]: TimeoutError: sync of todos took 45.0 s",
        "2026-10-12T12:00:00+0300 vps python[812]: TimeoutError: sync of todos took 30.9 s",
        "2026-10-12T12:06:00+0300 vps python[812]: KeyError: 'deadbeefcafe' in snapshot",
        "2026-10-12T12:05:00+0300 vps python[812]: KeyError: 'deadbeefcafe' in snapshot",
    ],
    "ai-dashboard": [
        "2026-10-12T09:00:00+0300 vps python[90]: token sk-ant-secret leaked in a log",
        "2026-10-12T10:00:00+0300 vps python[90]: token sk-ant-secret leaked in a log",
    ],
}


def _env() -> dict:
    previous = dict(os.environ)
    os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
    os.environ["YOUR_CHAT_ID"] = str(OWNER)
    return previous


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = _env()
        self.scan = importlib.import_module("ai_agent.log_scan")
        self.tasks = importlib.import_module("ai_agent.tasks")
        self.data: dict = {}

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    def run_scan(self, projects=("ai-pm-agent", "ai-dashboard"), now=NOW):
        return self.scan.scan(self.data, set(projects), now, lambda s: LINES.get(s, []))

    def test_lines_that_differ_in_numbers_and_ids_are_one_group(self) -> None:
        groups = self.scan.group_errors("ai-pm-agent", LINES["ai-pm-agent"])
        self.assertEqual([g.count for g in groups], [3, 2])
        self.assertEqual(groups[0].pattern, "TimeoutError: sync of todos took <n> s")
        self.assertEqual(groups[1].pattern, "KeyError: '<id>' in snapshot")
        self.assertEqual(len(groups[0].samples), 3)

    def test_proposes_the_most_frequent_errors_as_proposed_tasks(self) -> None:
        proposed = self.run_scan()
        self.assertEqual(len(proposed), 3)
        first = proposed[0]
        self.assertEqual(first["stage"], "proposed")
        self.assertEqual(first["repo"], "ai-pm-agent")
        self.assertEqual(
            first["title"],
            "Fix error in ai-pm-agent: TimeoutError: sync of todos took <n> s",
        )
        self.assertIn("Seen 3 time(s) in the last 24 hours", first["text"])
        self.assertIn("add a test that reproduces it", first["text"])
        self.assertEqual(first["note"], "seen 3× in 24 h in ai-pm-agent")
        view = self.tasks.view(first, self.data)
        self.assertEqual((view["source"], len(view["evidence"])), ("logs", 3))

    def test_services_without_a_project_are_skipped(self) -> None:
        proposed = self.run_scan(projects=("ai-dashboard",))
        self.assertEqual([task["repo"] for task in proposed], ["ai-dashboard"])

    def test_the_same_error_is_not_proposed_again_for_a_while(self) -> None:
        self.run_scan()
        self.assertEqual(self.run_scan(now=NOW + 3600), [])  # still open as proposals
        for task in list(self.tasks.tasks_of(self.data)):
            self.scan.dismiss(self.data, task, NOW)
            self.tasks.tasks_of(self.data).remove(task)
        self.assertEqual(self.run_scan(now=NOW + 7 * 86400), [])  # dismissed: quiet
        self.assertEqual(
            len(self.run_scan(now=NOW + 31 * 86400)), 3
        )  # quiet period over

    def test_once_a_day_and_secrets_are_redacted(self) -> None:
        self.assertTrue(self.scan.is_due(self.data, NOW))
        with patch.object(
            self.scan,
            "redact_sensitive",
            lambda text: text.replace("sk-ant-secret", "[redacted]"),
        ):
            proposed = self.run_scan(projects=("ai-dashboard",))
        self.assertFalse(self.scan.is_due(self.data, NOW + 3600))
        self.assertTrue(self.scan.is_due(self.data, NOW + 86400))
        self.assertNotIn("sk-ant-secret", str(proposed))
        self.assertNotIn("2026-10-12", str(proposed))

    def test_the_worker_never_starts_a_proposal(self) -> None:
        worker = importlib.import_module("ai_agent.worker")
        self.data["worker"] = {"enabled": True}
        self.run_scan()
        decision = worker.decide(self.data, self.tasks.tasks_of(self.data), {}, NOW)
        self.assertEqual(decision.status, "idle: no To do tasks")


class ProposalActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_env = _env()
        self.scan = importlib.import_module("ai_agent.log_scan")
        self.task_actions = importlib.import_module("ai_agent.bot.task_actions")
        self.loop = importlib.import_module("ai_agent.bot.worker_loop")
        self.errors = importlib.import_module("ai_agent.actions")
        self.data: dict = {"worker": {"enabled": True}}
        self.app = SimpleNamespace(
            user_data={OWNER: self.data},
            bot=SimpleNamespace(send_message=AsyncMock()),
            mark_data_for_update_persistence=MagicMock(),
        )
        self.no_todos = patch.object(
            self.loop.auto_todos, "is_due", lambda data, now: False
        )
        self.no_todos.start()

    def tearDown(self) -> None:
        self.no_todos.stop()
        os.environ.clear()
        os.environ.update(self.previous_env)

    async def test_accept_makes_it_todo_and_dismiss_keeps_it_away(self) -> None:
        first, second, _ = self.scan.scan(
            self.data, {"ai-pm-agent", "ai-dashboard"}, NOW, lambda s: LINES.get(s, [])
        )
        message = await self.task_actions.run_task_action(
            self.app, OWNER, "accept_task", {"task": first["id"]}
        )
        self.assertTrue(message.startswith("Accepted as To do"))
        self.assertEqual(first["stage"], "todo")
        with self.assertRaises(self.errors.ActionError):
            await self.task_actions.run_task_action(
                self.app, OWNER, "accept_task", {"task": first["id"]}
            )
        await self.task_actions.run_task_action(
            self.app, OWNER, "dismiss_task", {"task": second["id"]}
        )
        self.assertNotIn(second, self.data["tasks"])
        self.assertIn(second["signature"], self.data["log_scan"]["seen"])

    async def test_an_idle_worker_scans_once_a_day_and_says_so(self) -> None:
        journal = MagicMock(side_effect=lambda name: LINES.get(name, []))
        projects = [
            SimpleNamespace(name="ai-pm-agent"),
            SimpleNamespace(name="ai-dashboard"),
        ]
        with (
            patch.object(self.loop, "limits_snapshot", lambda: {}),
            patch.object(self.loop, "list_projects", lambda: projects),
            patch.object(self.loop.log_scan, "read_journal", journal),
        ):
            await self.loop.tick(self.app, OWNER, NOW)
            await self.loop.tick(self.app, OWNER, NOW + 60)
        self.assertEqual(
            sorted(call.args[0] for call in journal.call_args_list),
            ["ai-dashboard", "ai-pm-agent"],
        )
        said = self.app.bot.send_message.await_args.kwargs["text"]
        self.assertIn("proposed 3 task(s) from the server logs", said)
        stages = {task["stage"] for task in self.data["tasks"]}
        self.assertEqual(stages, {"proposed"})

    def test_requests_are_validated(self) -> None:
        inbox = importlib.import_module("ai_agent.inbox")
        inbox.validate({"id": "a1b2c3d4", "action": "worker_scan", "args": {}})
        inbox.validate(
            {"id": "a1b2c3d4", "action": "dismiss_task", "args": {"task": "0a1b2c3d"}}
        )
        with self.assertRaises(self.errors.ActionError):
            inbox.validate(
                {"id": "a1b2c3d4", "action": "worker_scan", "args": {"since": "1 week"}}
            )


def test_single_errors_do_not_become_recurring_proposals():
    from ai_agent import log_scan

    assert log_scan.scan({}, {"ai-dashboard"}, NOW, lambda _: ["One error"]) == []


def test_scan_history_is_persistent():
    from ai_agent.bot.state import PERSISTENT_KEYS

    assert "log_scan" in PERSISTENT_KEYS
