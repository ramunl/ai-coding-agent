"""The autonomous worker: what it picks, when it waits, how it stops."""

import asyncio
import importlib
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

OWNER = 123
NOW = 1_800_000_000.0


def _env() -> dict:
    previous = dict(os.environ)
    os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
    os.environ["YOUR_CHAT_ID"] = str(OWNER)
    return previous


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = _env()
        self.worker = importlib.import_module("ai_agent.worker")
        self.tasks = importlib.import_module("ai_agent.tasks")
        self.data: dict = {"worker": {"enabled": True}, "planning_agent": "codex"}
        self.task = self.tasks.add_task(self.data, "add login", "repo", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    def decide(self, limits: dict | None = None):
        return self.worker.decide(
            self.data, self.tasks.tasks_of(self.data), limits or {}, NOW
        )

    def approved_plan(self) -> None:
        self.task.update(stage="planned", plan_id="p1")
        self.data["pending_plan"] = SimpleNamespace(
            id="p1", approved=True, feature="add login"
        )
        self.data["pending_implementation"] = {"branch_name": "feature/login"}

    def test_off_by_default_and_when_switched_off(self) -> None:
        self.assertEqual(self.worker.decide({}, [], {}, NOW).status, "off")
        self.data["worker"]["enabled"] = False
        self.assertEqual(self.decide().action, "wait")

    def test_plans_the_oldest_todo_task_with_the_planner(self) -> None:
        self.tasks.add_task(self.data, "later", "repo", None)
        decision = self.decide()
        self.assertEqual(
            (decision.action, decision.task, decision.tool),
            ("plan", self.task, "codex"),
        )

    def test_skips_tasks_it_failed_and_says_when_idle(self) -> None:
        self.task["worker_failed"] = True
        self.assertEqual(self.decide().status, "idle: no To do tasks")

    def test_waits_for_your_approval_then_runs_the_approved_plan(self) -> None:
        self.task.update(stage="planned", plan_id="p1")
        self.data["pending_plan"] = SimpleNamespace(
            id="p1", approved=False, feature="add login"
        )
        self.assertEqual(self.decide().status, "waiting for your approval: add login")
        self.approved_plan()
        decision = self.decide()
        self.assertEqual((decision.action, decision.task), ("run", self.task))

    def test_never_starts_while_the_agent_is_busy(self) -> None:
        for busy in (
            {"planning_task": "x"},
            {"thinking": {"kind": "plan"}},
            {"active_execution": {"branch": "b"}},
            {"task_queue": [{"branch_name": "b"}]},
            {"pending_bugfix_clarification": {"bug": "x"}},
        ):
            with self.subTest(busy=busy):
                data = {**self.data, **busy}
                decision = self.worker.decide(
                    data, self.tasks.tasks_of(self.data), {}, NOW
                )
                self.assertEqual(decision.action, "wait")

    def test_daily_cap_counts_runs_in_the_last_24_hours(self) -> None:
        self.approved_plan()
        self.data["worker"].update(max_runs=2, runs=[NOW - 100, NOW - 90_000])
        self.assertEqual(self.decide().action, "run")  # one of the two is a day old
        self.data["worker"]["runs"].append(NOW - 50)
        self.assertEqual(self.decide().status, "waiting: 2 runs today, the daily cap")
        self.data["worker"]["max_runs"] = 0
        self.assertEqual(self.decide().action, "run")  # 0 = no cap

    def test_keeps_the_reserve_of_codex_limits(self) -> None:
        low = {
            "codex": {"windows": [{"remaining_percent": 20, "resets_at": NOW + 3600}]}
        }
        decision = self.decide(low)
        self.assertEqual(decision.action, "wait")
        self.assertIn(
            "codex has 20% left, below the 30% reserve until", decision.status
        )
        reset = {
            "codex": {"windows": [{"remaining_percent": 20, "resets_at": NOW - 60}]}
        }
        self.assertEqual(self.decide(reset).action, "plan")  # that window has reset
        self.data["worker"]["reserve"] = 10
        self.assertEqual(self.decide(low).action, "plan")
        self.data["planning_agent"] = "claude"  # no Claude Code reading: allowed
        self.data["worker"]["reserve"] = 30
        self.assertEqual(self.decide(low).action, "plan")

    def test_follow_up_reads_the_task_stage(self) -> None:
        worker = {"current": {"task": "x", "phase": "planning"}}
        self.assertIsNone(self.worker.follow_up(worker, {"stage": "planning"}))
        self.assertEqual(self.worker.follow_up(worker, {"stage": "planned"}), "ok")
        self.assertEqual(self.worker.follow_up(worker, {"stage": "todo"}), "failed")
        worker["current"]["phase"] = "running"
        self.assertIsNone(self.worker.follow_up(worker, {"stage": "implementing"}))
        self.assertEqual(self.worker.follow_up(worker, {"stage": "pr"}), "ok")
        self.assertEqual(self.worker.follow_up(worker, {"stage": "stopped"}), "failed")
        self.assertEqual(self.worker.follow_up(worker, None), "gone")


class TickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_env = _env()
        self.loop = importlib.import_module("ai_agent.bot.worker_loop")
        self.commands = importlib.import_module("ai_agent.bot.worker_commands")
        self.tasks = importlib.import_module("ai_agent.tasks")
        self.data: dict = {"worker": {"enabled": True}, "planning_agent": "codex"}
        self.task = self.tasks.add_task(self.data, "add login", "repo", None)
        self.app = SimpleNamespace(
            user_data={OWNER: self.data},
            bot=SimpleNamespace(send_message=AsyncMock()),
            mark_data_for_update_persistence=MagicMock(),
        )
        self.limits = patch.object(self.loop, "limits_snapshot", lambda: {})
        self.limits.start()

    def tearDown(self) -> None:
        self.limits.stop()
        os.environ.clear()
        os.environ.update(self.previous_env)

    def said(self) -> list[str]:
        return [
            call.kwargs["text"] for call in self.app.bot.send_message.await_args_list
        ]

    async def test_plans_a_task_and_tells_the_chat(self) -> None:
        start = AsyncMock(return_value="Planning with Codex...")
        with patch.object(self.loop.task_actions, "start", start):
            status = await self.loop.tick(self.app, OWNER, NOW)
        start.assert_awaited_once_with(self.app, OWNER, self.task)
        self.assertEqual(status, "planning add login")
        self.assertEqual(
            self.data["worker"]["current"],
            {"task": self.task["id"], "phase": "planning"},
        )
        self.assertIn("Approve the plan to let me run it", self.said()[0])

    async def test_runs_an_approved_plan_and_counts_it(self) -> None:
        self.task.update(stage="planned", plan_id="p1")
        self.data["pending_plan"] = SimpleNamespace(
            id="p1", approved=True, feature="add login"
        )
        self.data["pending_implementation"] = {"branch_name": "b"}
        run = AsyncMock(return_value="Queued task #1")
        with patch.object(self.loop.work_actions, "run_work_action", run):
            await self.loop.tick(self.app, OWNER, NOW)
        run.assert_awaited_once_with(self.app, OWNER, "confirm_work", {})
        self.assertEqual(self.data["worker"]["runs"], [NOW])
        self.assertEqual(self.data["worker"]["current"]["phase"], "running")

    async def test_three_failures_in_a_row_switch_it_off(self) -> None:
        for attempt in range(3):
            task = self.tasks.add_task(self.data, f"t{attempt}", "repo", None)
            self.tasks.set_stage(task, "todo", "planning failed: boom")
            self.data["worker"]["current"] = {"task": task["id"], "phase": "planning"}
            with patch.object(self.loop.task_actions, "start", AsyncMock()):
                await self.loop.tick(self.app, OWNER, NOW)
            self.assertTrue(task["worker_failed"])
        self.assertFalse(self.data["worker"]["enabled"])
        self.assertIn("switched off after 3 failed tasks", self.said()[-1])

    async def test_a_success_resets_the_failure_count(self) -> None:
        self.data["worker"].update(
            failures=2, current={"task": self.task["id"], "phase": "running"}
        )
        self.tasks.set_stage(self.task, "pr")
        await self.loop.tick(self.app, OWNER, NOW)
        self.assertEqual(self.data["worker"]["failures"], 0)

    async def test_a_refused_start_is_shown_as_waiting(self) -> None:
        refusing = AsyncMock(
            side_effect=self.loop.ActionError(
                "Cannot switch projects while 1 task(s) are queued."
            )
        )
        with patch.object(self.loop.task_actions, "start", refusing):
            status = await self.loop.tick(self.app, OWNER, NOW)
        self.assertEqual(
            status, "waiting: Cannot switch projects while 1 task(s) are queued."
        )
        self.assertIsNone(self.data["worker"]["current"])

    def test_switches_and_budget(self) -> None:
        data: dict = {}
        self.assertEqual(
            self.commands.run_worker_action(data, "worker_switch", {"value": "on"}),
            "Worker on.",
        )
        self.assertTrue(data["worker"]["enabled"])
        message = self.commands.run_worker_action(
            data, "worker_settings", {"max_runs": "0", "reserve": "15"}
        )
        self.assertEqual(
            message, "Worker budget: no cap runs a day, keeps 15% of limits."
        )
        view = importlib.import_module("ai_agent.worker").view(data, time.time())
        self.assertEqual(
            (view["enabled"], view["max_runs"], view["reserve"]), (True, 0, 15)
        )

    def test_dashboard_requests_are_validated(self) -> None:
        inbox = importlib.import_module("ai_agent.inbox")
        errors = importlib.import_module("ai_agent.actions")

        def request(action: str, args: dict) -> dict:
            return {"id": "a1b2c3d4", "action": action, "args": args}

        inbox.validate(request("worker_settings", {"max_runs": "50", "reserve": "90"}))
        for action, args in (
            ("worker_switch", {"value": "yes"}),
            ("worker_settings", {"max_runs": "51", "reserve": "10"}),
            ("worker_settings", {"max_runs": "3", "reserve": "95"}),
            ("worker_settings", {"max_runs": "-1", "reserve": "10"}),
        ):
            with self.assertRaises(errors.ActionError, msg=str(args)):
                inbox.validate(request(action, args))

    async def test_worker_command(self) -> None:
        replies: list[str] = []

        async def capture(update, text):
            replies.append(text)

        context = SimpleNamespace(args=["budget", "5", "20%"], user_data={})
        with (
            patch.object(self.commands, "require_authorized", lambda update: True),
            patch.object(self.commands, "reply_chunks", capture),
        ):
            await self.commands.worker_cmd(None, context)
            context.args = ["budget", "99", "20"]
            await self.commands.worker_cmd(None, context)
            context.args = ["on"]
            await self.commands.worker_cmd(None, context)
        self.assertIn("Runs today: 0 (cap 5)", replies[0])
        self.assertIn("Reserve: 20%", replies[0])
        self.assertIn("0–50", replies[1])
        self.assertTrue(context.user_data["worker"]["enabled"])

    async def test_loop_survives_a_failing_tick(self) -> None:
        with (
            patch.object(
                self.loop, "tick", AsyncMock(side_effect=RuntimeError("boom"))
            ),
            self.assertLogs("ai_agent.bot.worker_loop", "WARNING"),
        ):
            task = asyncio.create_task(
                self.loop.worker_forever(self.app, OWNER, interval=0.01)
            )
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
