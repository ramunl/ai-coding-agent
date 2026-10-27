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

    def test_an_autopilot_task_is_approved_by_the_worker(self) -> None:
        self.task.update(stage="planned", plan_id="p1", autopilot=True)
        self.data["pending_plan"] = SimpleNamespace(
            id="p1", approved=False, feature="add login"
        )
        decision = self.decide()
        self.assertEqual((decision.action, decision.task), ("approve", self.task))
        self.assertEqual(decision.status, "approving add login (autopilot)")
        self.task["autopilot_stuck"] = True
        self.assertEqual(self.decide().action, "wait")

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
        self.data["worker"]["auto_tool"] = False
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

    def test_switches_to_the_other_tool_when_one_has_no_room(self) -> None:
        low = {
            "codex": {"windows": [{"remaining_percent": 20, "resets_at": NOW + 3600}]}
        }
        decision = self.decide(low)
        self.assertEqual((decision.action, decision.tool), ("plan", "claude"))
        self.assertIn("with claude (codex has 20% left", decision.status)
        self.data["worker"]["out_until"] = {"claude": NOW + 600}
        decision = self.decide(low)
        self.assertEqual(decision.action, "wait")
        self.assertIn("codex has 20% left", decision.status)
        self.assertIn("claude hit its usage limit; out until", decision.status)
        self.data["worker"]["out_until"] = {"claude": NOW - 1}  # over
        self.assertEqual(self.decide(low).tool, "claude")

    def test_the_run_uses_the_implementer_with_room(self) -> None:
        self.approved_plan()
        self.data["implementation_agent"] = "claude"
        self.data["worker"]["out_until"] = {"claude": NOW + 600}
        decision = self.decide()
        self.assertEqual((decision.action, decision.tool), ("run", "codex"))

    def test_limit_errors_and_their_reset_times(self) -> None:
        reset = self.worker.limit_reset
        self.assertIsNone(reset("The implementation agent made no file changes", NOW))
        self.assertIsNone(reset("", NOW))
        self.assertEqual(
            reset("Claude AI usage limit reached|1800003600", NOW), 1800003600
        )
        self.assertEqual(
            reset("You've hit your usage limit. Try again in 2 hours.", NOW), NOW + 7200
        )
        self.assertEqual(
            reset("Rate limit exceeded, retry in 15 minutes", NOW), NOW + 900
        )
        at_six = reset("5-hour limit reached ∙ resets 6pm", NOW)
        self.assertEqual(time.localtime(at_six).tm_hour, 18)
        self.assertTrue(NOW < at_six <= NOW + 24 * 3600)
        self.assertEqual(reset("usage limit reached", NOW), NOW + 3600)  # no time given

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
        # No PM bridge in tests: the #auto todo scan is tested on its own.
        self.no_todos = patch.object(
            self.loop.auto_todos, "is_due", lambda data, now: False
        )
        self.no_todos.start()
        self.no_reviews = patch.object(
            self.loop.code_review, "is_due", lambda data, now: False
        )
        self.no_reviews.start()

    def tearDown(self) -> None:
        self.limits.stop()
        self.no_todos.stop()
        self.no_reviews.stop()
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
        start.assert_awaited_once_with(self.app, OWNER, self.task, "codex")
        self.assertEqual(status, "planning add login with codex")
        current = self.data["worker"]["current"]
        self.assertEqual(
            (current["task"], current["phase"], current["tool"]),
            (self.task["id"], "planning", "codex"),
        )
        self.assertIn("Approve the plan to let me run it", self.said()[0])

    async def test_a_queued_task_starts_first_even_with_the_worker_off(self) -> None:
        self.data["worker"]["enabled"] = False
        other = self.tasks.add_task(self.data, "fix popup", "repo", None)
        other["start_next"] = NOW
        start = AsyncMock(return_value="Planning...")
        with patch.object(self.loop.task_actions, "start", start):
            status = await self.loop.tick(self.app, OWNER, NOW)
        start.assert_awaited_once_with(self.app, OWNER, other)
        self.assertEqual(status, "started your queued task “fix popup”")
        self.assertIn("started your queued task “fix popup” in repo", self.said()[0])

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

    async def test_the_run_keeps_the_chosen_implementer_for_this_task_only(
        self,
    ) -> None:
        self.task.update(stage="planned", plan_id="p1")
        self.data["pending_plan"] = SimpleNamespace(
            id="p1", approved=True, feature="add login"
        )
        self.data["pending_implementation"] = {"branch_name": "b"}
        self.data["implementation_agent"] = "claude"
        self.data["worker"]["out_until"] = {"claude": NOW + 600}
        with patch.object(self.loop.work_actions, "run_work_action", AsyncMock()):
            await self.loop.tick(self.app, OWNER, NOW)
        self.assertEqual(
            self.data["pending_implementation"]["implementation_agent"], "codex"
        )
        self.assertEqual(
            self.data["implementation_agent"], "claude"
        )  # your setting stays

    async def test_a_limit_failure_marks_the_tool_out_and_retries_the_task(
        self,
    ) -> None:
        self.tasks.set_stage(
            self.task, "stopped", "the run ended without a pull request"
        )
        self.data["worker"]["current"] = {
            "task": self.task["id"],
            "phase": "running",
            "tool": "claude",
            "started_at": NOW - 60,
        }
        self.data["last_work_error"] = {
            "text": "Claude AI usage limit reached|1800003600",
            "at": NOW - 10,
        }
        with patch.object(self.loop.task_actions, "start", AsyncMock()) as start:
            await self.loop.tick(self.app, OWNER, NOW)
        self.assertEqual(self.data["worker"]["out_until"]["claude"], 1800003600)
        self.assertEqual(self.data["worker"]["failures"], 0)
        self.assertNotIn("worker_failed", self.task)
        self.assertIn("claude hit its usage limit until", self.said()[0])
        self.assertIn("using codex meanwhile", self.said()[0])
        # Back to To do, and picked again at once with the other tool.
        start.assert_awaited_once_with(self.app, OWNER, self.task, "codex")

    async def test_an_old_error_does_not_count_as_this_runs_failure(self) -> None:
        self.tasks.set_stage(self.task, "stopped", "no plan was made")
        self.data["worker"]["current"] = {
            "task": self.task["id"],
            "phase": "running",
            "tool": "claude",
            "started_at": NOW - 60,
        }
        self.data["last_work_error"] = {"text": "usage limit reached", "at": NOW - 3600}
        await self.loop.tick(self.app, OWNER, NOW)
        self.assertEqual(self.data["worker"]["failures"], 1)
        self.assertEqual(self.data["worker"]["out_until"], {})

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

    async def test_finished_steps_are_kept_as_daily_stats(self) -> None:
        self.data["worker"].update(
            current={"task": self.task["id"], "phase": "running", "tool": "claude"}
        )
        self.tasks.set_stage(self.task, "pr")
        await self.loop.tick(self.app, OWNER, NOW)
        failed = self.tasks.add_task(self.data, "fix cache", "repo", None)
        self.tasks.set_stage(failed, "todo", "planning failed: boom")
        self.data["worker"]["current"] = {
            "task": failed["id"],
            "phase": "planning",
            "tool": "codex",
        }
        old = {"at": NOW - 8 * 24 * 3600, "outcome": "pr", "tool": "codex"}
        self.data["worker"]["history"].insert(0, old)
        with patch.object(self.loop.task_actions, "start", AsyncMock()):
            await self.loop.tick(self.app, OWNER, NOW + 60)
        view = importlib.import_module("ai_agent.worker").view(self.data, NOW + 60)
        today = view["stats"][0]
        self.assertEqual(len(view["stats"]), 7)
        self.assertEqual((today["pr"], today["failed"], today["planned"]), (1, 1, 0))
        self.assertEqual(today["tools"], {"claude": 1, "codex": 1})
        self.assertEqual(
            [(item["title"], item["outcome"]) for item in view["recent"]],
            [("fix cache", "failed"), ("add login", "pr")],
        )
        self.assertNotIn(old, self.data["worker"]["history"])  # older than 7 days

    async def test_autopilot_approves_or_hands_the_plan_back(self) -> None:
        self.task.update(stage="planned", plan_id="p1", autopilot=True)
        plan = SimpleNamespace(id="p1", approved=False, feature="add login")
        self.data["pending_plan"] = plan

        async def approve(app, owner, action, args) -> str:
            self.assertEqual(action, "approve_plan")
            plan.approved = True
            return "Plan approved."

        with patch.object(self.loop.work_actions, "run_work_action", approve):
            await self.loop.tick(self.app, OWNER, NOW)
        self.assertTrue(plan.approved)
        self.assertIn("approved “add login” (autopilot)", self.said()[-1])

        plan.approved = False
        refuse = AsyncMock(return_value="This plan does not change repo")
        with patch.object(self.loop.work_actions, "run_work_action", refuse):
            await self.loop.tick(self.app, OWNER, NOW)
        self.assertTrue(self.task["autopilot_stuck"])
        self.assertIn("waits for you", self.said()[-1])

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

    def test_review_setting(self) -> None:
        data: dict = {}
        message = self.commands.run_worker_action(
            data, "worker_reviews", {"value": "5"}
        )
        self.assertIn("up to 5 proposals", message)
        view = importlib.import_module("ai_agent.worker").view(data, time.time())
        self.assertEqual(view["review_proposals"], 5)
        self.assertIn(
            "off",
            self.commands.run_worker_action(data, "worker_reviews", {"value": "0"}),
        )
        inbox = importlib.import_module("ai_agent.inbox")
        inbox.validate(
            {"id": "a1b2c3d4", "action": "worker_reviews", "args": {"value": "10"}}
        )
        inbox.validate({"id": "a1b2c3d4", "action": "worker_review", "args": {}})
        with self.assertRaises(importlib.import_module("ai_agent.actions").ActionError):
            inbox.validate(
                {"id": "a1b2c3d4", "action": "worker_reviews", "args": {"value": "11"}}
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
            context.args = ["tools", "fixed"]
            await self.commands.worker_cmd(None, context)
        self.assertIn("Runs today: 0 (cap 5)", replies[0])
        self.assertIn("Reserve: 20%", replies[0])
        self.assertIn("0–50", replies[1])
        self.assertTrue(context.user_data["worker"]["enabled"])
        self.assertFalse(context.user_data["worker"]["auto_tool"])
        self.assertIn("Tools: chosen ones only", replies[-1])

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
