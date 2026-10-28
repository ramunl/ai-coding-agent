"""Tasks made from todos: their stages, and the dashboard's task requests."""

import asyncio
import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

OWNER = 123


def _env() -> dict:
    previous = dict(os.environ)
    os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
    os.environ["YOUR_CHAT_ID"] = str(OWNER)
    return previous


class StageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = _env()
        self.tasks = importlib.import_module("ai_agent.tasks")
        self.data: dict = {}
        self.task = self.tasks.add_task(self.data, "add  login", "repo", "todos:abc1")
        self.task.update(plan_id="p1", branch="feature/login")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    def plan(self, plan_id: str = "p1") -> SimpleNamespace:
        return SimpleNamespace(id=plan_id, revision=2, approved=False)

    def test_new_task_is_todo_with_a_short_id(self) -> None:
        self.assertEqual(self.task["stage"], "todo")
        self.assertEqual(self.task["title"], "add login")
        self.assertRegex(self.task["id"], r"^[0-9a-f]{8}$")
        self.assertIs(self.tasks.find_task(self.data, self.task["id"]), self.task)

    def test_planning_without_a_live_run_falls_back_to_todo(self) -> None:
        self.tasks.set_stage(self.task, "planning")
        self.data["planning_task"] = self.task["id"]
        self.assertFalse(self.tasks.sync(self.data))
        self.data.pop("planning_task")  # e.g. after a restart
        self.assertTrue(self.tasks.sync(self.data))
        self.assertEqual(self.task["stage"], "todo")
        self.assertEqual(self.task["note"], "planning was interrupted")

    def test_planned_follows_its_branch_into_the_queue_then_checks_github(self) -> None:
        self.tasks.set_stage(self.task, "planned")
        self.data["pending_plan"] = self.plan()
        self.assertFalse(self.tasks.sync(self.data))
        self.data.pop("pending_plan")
        self.data["task_queue"] = [{"branch_name": "feature/login"}]
        self.tasks.sync(self.data)
        self.assertEqual(self.task["stage"], "implementing")
        self.data["task_queue"] = []
        self.data["active_execution"] = {"branch": "feature/login"}
        self.assertFalse(self.tasks.sync(self.data))
        self.data.pop("active_execution")
        self.data["last_execution"] = SimpleNamespace(
            branch="feature/login", pr_url="https://github.com/o/r/pull/7"
        )
        self.tasks.sync(self.data)
        self.assertEqual(self.task["stage"], "ended")
        self.assertIsNone(self.task["pr_url"])

    def test_a_run_that_ends_unseen_is_not_called_pr_open(self) -> None:
        self.tasks.set_stage(self.task, "implementing")
        self.data["last_execution"] = SimpleNamespace(branch="other", pr_url="u")
        self.tasks.sync(self.data)
        self.assertEqual(self.task["stage"], "ended")
        self.assertEqual(self.task["note"], self.tasks.ENDED_NOTE)

    def test_interrupted_retry_does_not_reuse_an_old_same_branch_result(self) -> None:
        self.task.update(stage="implementing", attempt=2)
        self.data["last_execution"] = SimpleNamespace(
            branch="feature/login", pr_url="https://github.com/o/r/pull/7"
        )
        self.assertTrue(self.tasks.sync(self.data))
        self.assertEqual(self.task["stage"], "ended")
        self.assertIsNone(self.task["pr_url"])
        pulls = importlib.import_module("ai_agent.task_pulls")
        self.assertTrue(pulls.apply_pull(self.task, None))
        self.assertFalse(self.tasks.sync(self.data))
        self.assertEqual(self.task["stage"], "stopped")

    def test_tasks_saved_as_pr_open_by_the_old_guess_are_moved_to_ended(self) -> None:
        self.tasks.set_stage(self.task, "pr", "run finished; pull request not recorded")
        self.assertTrue(self.tasks.sync(self.data))
        self.assertEqual(self.task["stage"], "ended")

    def test_cancelled_or_replaced_plan_stops_the_task(self) -> None:
        self.tasks.set_stage(self.task, "planned")
        self.data["pending_plan"] = self.plan("other")
        self.tasks.sync(self.data)
        self.assertEqual(self.task["stage"], "stopped")

    def test_run_without_a_recorded_pull_request_checks_github(self) -> None:
        self.tasks.set_stage(self.task, "implementing")
        self.data["last_execution"] = SimpleNamespace(
            branch="feature/login", pr_url=None
        )
        self.tasks.sync(self.data)
        self.assertEqual(self.task["stage"], "ended")
        self.assertEqual(self.task["note"], self.tasks.ENDED_NOTE)

    def test_view_shows_the_plan_only_for_the_tasks_own_plan(self) -> None:
        self.tasks.set_stage(self.task, "planned")
        self.data["pending_plan"] = self.plan()
        view = self.tasks.view(self.task, self.data)
        self.assertEqual((view["plan_revision"], view["plan_approved"]), (2, False))
        self.data["pending_plan"] = self.plan("other")
        self.assertIsNone(self.tasks.view(self.task, self.data)["plan_revision"])

    def test_list_is_capped_by_dropping_finished_tasks_first(self) -> None:
        for _ in range(self.tasks.MAX_TASKS + 5):
            done = self.tasks.add_task(self.data, "old", "repo", None)
            self.tasks.set_stage(done, "pr")
        tasks = self.tasks.tasks_of(self.data)
        self.assertEqual(len(tasks), self.tasks.MAX_TASKS)
        self.assertIn(self.task, tasks)  # still to do, so kept

    def test_tasks_are_saved_and_published(self) -> None:
        state = importlib.import_module("ai_agent.bot.state")
        self.assertIn("tasks", state.PERSISTENT_KEYS)
        self.assertIn("planning_task", state.TRANSIENT_KEYS)
        published = state.snapshot(self.data)["tasks"]
        self.assertEqual(published[0]["title"], "add login")
        self.assertEqual(published[0]["todo"], "todos:abc1")


class TaskActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_env = _env()
        self.task_actions = importlib.import_module("ai_agent.bot.task_actions")
        self.work = importlib.import_module("ai_agent.bot.work_actions")
        self.errors = importlib.import_module("ai_agent.actions")
        self.inbox = importlib.import_module("ai_agent.inbox")
        plan_state = importlib.import_module("ai_agent.plan_state")
        self.PlanState = plan_state.PlanState
        self.data: dict = {}
        self.app = SimpleNamespace(
            user_data={OWNER: self.data},
            bot=SimpleNamespace(send_message=AsyncMock(), set_my_name=AsyncMock()),
            mark_data_for_update_persistence=MagicMock(),
        )
        self.active = SimpleNamespace(name="repo")
        self.patches = [
            patch.object(self.task_actions, "active_project", lambda: self.active),
            patch.object(
                self.task_actions,
                "get_project",
                lambda name: SimpleNamespace(name=name),
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.work._starting.clear()
        os.environ.clear()
        os.environ.update(self.previous_env)

    async def run_action(self, action: str, args: dict) -> str:
        return await self.task_actions.run_task_action(self.app, OWNER, action, args)

    def fake_plan(self, release: asyncio.Event | None = None):
        async def plan(update, context) -> None:
            await update.message.reply_text("Planning with Claude...")
            if release:
                await release.wait()
            context.user_data["pending_plan"] = self.PlanState(
                id="p9",
                feature=" ".join(context.args),
                revision=1,
                plan_text="{}",
                approved=False,
            )

        return plan

    async def test_make_task_plans_it_and_ties_the_plan_to_it(self) -> None:
        release = asyncio.Event()
        with patch.object(self.task_actions, "plan", self.fake_plan(release)):
            result = await self.run_action(
                "create_task",
                {"repo": "repo", "text": "add login", "todo": "todos:abc1"},
            )
            self.assertEqual(result, "Task added. Planning with Claude...")
            task = self.data["tasks"][0]
            self.assertEqual(task["stage"], "planning")
            self.assertEqual(self.data["planning_task"], task["id"])
            release.set()
            await asyncio.sleep(0.02)
        self.assertEqual(task["stage"], "planned")
        self.assertEqual(task["plan_id"], "p9")
        self.assertTrue(task["branch"])
        self.assertNotIn("planning_task", self.data)

    async def test_failed_planning_puts_the_task_back_to_do(self) -> None:
        async def failing_plan(update, context) -> None:
            await update.message.reply_text("Planning with Claude...")
            raise RuntimeError("planner down")

        with patch.object(self.task_actions, "plan", failing_plan):
            await self.run_action(
                "create_task", {"repo": "repo", "text": "x", "todo": "-"}
            )
            await asyncio.sleep(0.02)
        task = self.data["tasks"][0]
        self.assertEqual(task["stage"], "todo")
        self.assertIsNone(task["todo"])
        self.assertEqual(task["note"], "planner down")
        sent = [c.kwargs["text"] for c in self.app.bot.send_message.await_args_list]
        self.assertEqual(sent, ["Planning with Claude...", "Failed: planner down"])

    async def test_planning_that_makes_no_plan_says_so(self) -> None:
        async def silent_plan(update, context) -> None:
            await update.message.reply_text("Planning with Claude...")

        with patch.object(self.task_actions, "plan", silent_plan):
            await self.run_action(
                "create_task", {"repo": "repo", "text": "x", "todo": "-"}
            )
            await asyncio.sleep(0.02)
        self.assertEqual(
            self.data["tasks"][0]["note"], "no plan was made; see the bot chat"
        )

    async def test_a_planner_override_lasts_for_one_plan(self) -> None:
        seen = {}

        async def plan(update, context) -> None:
            seen["planner"] = context.user_data.get("planning_agent")
            await update.message.reply_text("Planning with Codex...")

        self.data["planning_agent"] = "claude"
        task = importlib.import_module("ai_agent.tasks").add_task(
            self.data, "x", "repo", None
        )
        with patch.object(self.task_actions, "plan", plan):
            await self.task_actions.start(self.app, OWNER, task, "codex")
            await asyncio.sleep(0.02)
        self.assertEqual(seen["planner"], "codex")
        self.assertEqual(self.data["planning_agent"], "claude")

    async def test_a_busy_agent_queues_the_new_task(self) -> None:
        self.data["pending_plan"] = self.PlanState(
            id="p1", feature="other", revision=1, plan_text="{}", approved=False
        )
        result = await self.run_action(
            "create_task", {"repo": "repo", "text": "add login", "todo": "-"}
        )
        self.assertEqual(
            result,
            "Task added. Queued: “add login” starts when the agent is free "
            "(now a plan is waiting for approval or confirmation).",
        )
        task = self.data["tasks"][0]
        self.assertEqual(task["stage"], "todo")
        self.assertTrue(task["start_next"])
        self.assertTrue(
            importlib.import_module("ai_agent.tasks").view(task, self.data)["queued"]
        )
        self.app.bot.send_message.assert_not_awaited()

    async def test_another_repo_is_switched_to_once_queued_work_is_done(
        self,
    ) -> None:
        self.data["task_queue"] = [{"branch_name": "b"}]
        result = await self.run_action(
            "create_task", {"repo": "other", "text": "x", "todo": "-"}
        )
        self.assertIn(
            "starts when the agent is free (now 1 task(s) are queued)", result
        )
        task = self.data["tasks"][0]
        # Asking again while busy keeps it queued, with its first place in line.
        first = task["start_next"]
        self.assertIn(
            "Queued", await self.run_action("start_task", {"task": task["id"]})
        )
        self.assertEqual(task["start_next"], first)
        self.assertIsNone(await self.task_actions.start_queued(self.app, OWNER))
        self.assertEqual(task["stage"], "todo")
        self.data["task_queue"] = []
        use = AsyncMock(return_value=SimpleNamespace(name="other"))
        with (
            patch.object(self.task_actions, "use_project", use),
            patch.object(self.task_actions, "plan", self.fake_plan()),
        ):
            started = await self.task_actions.start_queued(self.app, OWNER)
            await asyncio.sleep(0.02)
        self.assertIs(started, task)
        self.assertNotIn("start_next", task)
        use.assert_awaited_once_with("other", self.data)
        self.app.bot.set_my_name.assert_awaited()

    async def test_a_queued_start_can_be_cancelled(self) -> None:
        self.data["pending_plan"] = self.PlanState(
            id="p1", feature="other", revision=1, plan_text="{}", approved=False
        )
        await self.run_action("create_task", {"repo": "repo", "text": "x", "todo": "-"})
        task = self.data["tasks"][0]
        self.assertEqual(
            await self.run_action("cancel_start", {"task": task["id"]}),
            "Not queued any more: “x” stays To do.",
        )
        self.assertNotIn("start_next", task)
        self.assertIsNone(task["note"])
        with self.assertRaisesRegex(self.errors.ActionError, "not queued"):
            await self.run_action("cancel_start", {"task": task["id"]})
        self.inbox.validate(
            {"id": "a1b2c3d4", "action": "cancel_start", "args": {"task": task["id"]}}
        )

    async def test_start_and_remove_respect_the_stage(self) -> None:
        tasks = importlib.import_module("ai_agent.tasks")
        task = tasks.add_task(self.data, "x", "repo", None)
        tasks.set_stage(task, "implementing")
        for action in ("start_task", "remove_task"):
            with self.assertRaises(self.errors.ActionError, msg=action):
                await self.run_action(action, {"task": task["id"]})
        tasks.set_stage(task, "stopped")
        self.assertEqual(
            await self.run_action("remove_task", {"task": task["id"]}),
            "Task removed: x",
        )
        with self.assertRaises(self.errors.ActionError):
            await self.run_action("remove_task", {"task": task["id"]})

    async def test_a_waiting_part_cannot_start_and_split_needs_a_plan(self) -> None:
        tasks = importlib.import_module("ai_agent.tasks")
        first = tasks.add_task(self.data, "part one", "repo", None)
        tasks.set_stage(first, "implementing")
        second = tasks.add_task(self.data, "part two", "repo", None)
        second["after"] = first["id"]
        with self.assertRaisesRegex(self.errors.ActionError, "waits for “part one”"):
            await self.run_action("start_task", {"task": second["id"]})
        with self.assertRaisesRegex(self.errors.ActionError, "No pending plan"):
            await self.run_action("split_plan", {})
        self.inbox.validate({"id": "a1b2c3d4", "action": "split_plan", "args": {}})

    async def test_a_failed_run_is_retried_with_another_tool(self) -> None:
        tasks = importlib.import_module("ai_agent.tasks")
        task = tasks.add_task(self.data, "upload images", "repo", None)
        task.update(branch="feature/upload")
        tasks.set_stage(task, "planned")
        state = importlib.import_module("ai_agent.bot.state")
        item = state.enqueue_pending_implementation(
            self.data,
            {
                "change": "upload images",
                "images": [{"id": "a1b2.png"}],
                "codex_prompt": "Implement uploads",
                "branch_name": "feature/upload",
                "implementation_agent": "codex",
            },
        )
        self.assertEqual(task["run"]["codex_prompt"], "Implement uploads")
        self.data["task_queue"].remove(item)
        with self.assertRaisesRegex(self.errors.ActionError, "no failed run"):
            await self.run_action("retry_task", {"task": task["id"], "tool": "claude"})
        tasks.set_stage(task, "stopped", "the run ended without a pull request")
        self.assertTrue(tasks.view(task, self.data)["can_retry"])
        seen = {}

        async def confirm(app, owner, action, args) -> str:
            seen["action"] = action
            seen["pending"] = dict(self.data["pending_implementation"])
            return "Queued task #2"

        with patch.object(self.work, "run_work_action", confirm):
            reply = await self.run_action(
                "retry_task", {"task": task["id"], "tool": "claude"}
            )
        self.assertEqual(seen["action"], "confirm_work")
        self.assertEqual(seen["pending"]["implementation_agent"], "claude")
        self.assertEqual(seen["pending"]["branch_name"], "feature/upload")
        self.assertEqual(seen["pending"]["images"], [{"id": "a1b2.png"}])
        self.assertEqual(task["stage"], "implementing")
        self.assertIn("Retrying", reply)

    async def test_a_retry_waits_while_other_work_runs(self) -> None:
        tasks = importlib.import_module("ai_agent.tasks")
        task = tasks.add_task(self.data, "x", "repo", None)
        task["run"] = {"branch_name": "feature/x", "codex_prompt": "p"}
        tasks.set_stage(task, "stopped")
        self.data["active_execution"] = {"branch": "feature/other"}
        with self.assertRaisesRegex(self.errors.ActionError, "a task is running"):
            await self.run_action("retry_task", {"task": task["id"], "tool": "codex"})
        self.assertEqual(task["stage"], "stopped")
        self.inbox.validate(
            {
                "id": "a1b2c3d4",
                "action": "retry_task",
                "args": {"task": task["id"], "tool": "claude"},
            }
        )
        with self.assertRaises(Exception):
            self.inbox.validate(
                {
                    "id": "a1b2c3d4",
                    "action": "retry_task",
                    "args": {"task": task["id"], "tool": "gpt"},
                }
            )

    async def test_autopilot_is_set_on_create_or_switched_later(self) -> None:
        self.work._starting.add(object())  # busy: the new task stays To do
        await self.run_action(
            "create_autopilot_task", {"repo": "repo", "text": "add login", "todo": "-"}
        )
        tasks = importlib.import_module("ai_agent.tasks")
        task = tasks.tasks_of(self.data)[-1]
        self.assertTrue(task["autopilot"])
        self.assertTrue(tasks.view(task, self.data)["autopilot"])
        task["autopilot_stuck"] = True
        reply = await self.run_action(
            "set_autopilot", {"task": task["id"], "value": "off"}
        )
        self.assertFalse(task["autopilot"])
        self.assertNotIn("autopilot_stuck", task)
        self.assertIn("waits for your approval", reply)
        for action, args in (
            ("set_autopilot", {"task": task["id"], "value": "on"}),
            ("create_autopilot_task", {"repo": "repo", "text": "x", "todo": "-"}),
        ):
            self.inbox.validate({"id": "a1b2c3d4", "action": action, "args": args})

    async def test_unknown_repository_adds_nothing(self) -> None:
        def missing(name: str) -> None:
            raise self.task_actions.ProjectError(f"Unknown project '{name}'.")

        with (
            patch.object(self.task_actions, "get_project", missing),
            self.assertRaises(self.errors.ActionError),
        ):
            await self.run_action(
                "create_task", {"repo": "nope", "text": "x", "todo": "-"}
            )
        self.assertEqual(self.data.get("tasks", []), [])

    def test_inbox_validates_task_requests(self) -> None:
        def request(action: str, args: dict) -> dict:
            return {"id": "a1b2c3d4", "action": action, "args": args}

        self.inbox.validate(
            request(
                "create_task",
                {"repo": "repo", "text": "x", "todo": "my_ai_agents:3f2a"},
            )
        )
        self.inbox.validate(request("start_task", {"task": "0a1b2c3d"}))
        for action, args in (
            ("create_task", {"repo": "../x", "text": "x", "todo": "-"}),
            ("create_task", {"repo": "repo", "text": "x", "todo": "a:b:c"}),
            ("create_task", {"repo": "repo", "text": "x"}),
            ("start_task", {"task": "0A1B2C3D"}),
            ("remove_task", {"task": "123"}),
        ):
            with self.assertRaises(self.errors.ActionError, msg=str(args)):
                self.inbox.validate(request(action, args))


def test_task_keeps_full_instructions_beyond_display_title():
    from ai_agent.tasks import add_task

    text = "Describe the change " * 40
    task = add_task({}, text, "ai-dashboard", None)
    assert len(task["title"]) == 300
    assert task["text"] == " ".join(text.split())
