"""Dashboard work requests: approve, confirm, cancel and remove queued."""

import asyncio
import importlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

OWNER = 123


class WorkActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = str(OWNER)
        self.work = importlib.import_module("ai_agent.bot.work_actions")
        self.inbox = importlib.import_module("ai_agent.inbox")
        self.actions = importlib.import_module("ai_agent.actions")
        plan_state = importlib.import_module("ai_agent.plan_state")
        self.plan = plan_state.PlanState(
            id="p1", feature="add login", revision=1, plan_text="Do it.", approved=False
        )
        self.data: dict = {}
        self.app = SimpleNamespace(
            user_data={OWNER: self.data}, bot=SimpleNamespace(send_message=AsyncMock())
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    async def run_action(self, action: str, args: dict | None = None) -> str:
        return await self.work.run_work_action(self.app, OWNER, action, args or {})

    def sent(self) -> list[str]:
        return [
            call.kwargs["text"] for call in self.app.bot.send_message.await_args_list
        ]

    async def test_approve_runs_the_command_and_answers_in_the_owner_chat(self) -> None:
        self.data["pending_plan"] = self.plan
        result = await self.run_action("approve_plan")
        self.assertTrue(self.data["pending_plan"].approved)
        self.assertIn("pending_implementation", self.data)
        self.assertTrue(result.startswith("Plan approved."))
        self.assertNotIn("\n", result)
        call = self.app.bot.send_message.await_args
        self.assertEqual(call.kwargs["chat_id"], OWNER)
        self.assertIn("Plan approved.", call.kwargs["text"])

    async def test_stale_requests_are_refused_before_any_handler_runs(self) -> None:
        cases = [
            ("approve_plan", {}, "no plan"),
            ("confirm_work", {}, "nothing to confirm"),
            ("cancel_pending", {}, "nothing pending"),
            ("remove_queued", {"task": "7"}, "not in the queue"),
        ]
        for action, args, expected in cases:
            with self.assertRaises(self.actions.ActionError) as raised:
                await self.run_action(action, args)
            self.assertIn(expected, str(raised.exception))
        self.data["pending_plan"] = self.plan
        with self.assertRaises(self.actions.ActionError) as raised:
            await self.run_action("confirm_work")
        self.assertIn("not approved", str(raised.exception))
        self.app.bot.send_message.assert_not_awaited()

    async def test_cancel_discards_pending_work(self) -> None:
        self.data.update(pending_plan=self.plan, pending_implementation={"x": 1})
        result = await self.run_action("cancel_pending")
        self.assertNotIn("pending_plan", self.data)
        self.assertNotIn("pending_implementation", self.data)
        self.assertTrue(result.startswith("Pending request discarded."))

    async def test_remove_queued_takes_out_only_that_task(self) -> None:
        self.data["task_queue"] = [
            {"id": 3, "branch_name": "feat/a"},
            {"id": 4, "branch_name": "feat/b"},
        ]
        result = await self.run_action("remove_queued", {"task": "4"})
        self.assertEqual([task["id"] for task in self.data["task_queue"]], [3])
        self.assertIn("Queued task #4 removed.", result)

    async def test_confirm_answers_after_the_first_reply_and_keeps_running(
        self,
    ) -> None:
        finished = asyncio.Event()

        async def slow_confirm(update, context) -> None:
            await update.message.reply_text(
                "Queued task #1 at position 1.\n\nBranch:\nx"
            )
            await asyncio.sleep(0.2)
            finished.set()

        self.data["pending_implementation"] = {"branch_name": "x"}
        with patch.object(self.work, "confirm", slow_confirm):
            started = time.monotonic()
            result = await self.run_action("confirm_work")
            self.assertLess(time.monotonic() - started, 0.15)
            self.assertEqual(result, "Queued task #1 at position 1. Branch: x")
            self.assertFalse(finished.is_set())
            await asyncio.wait_for(finished.wait(), 1)

    async def test_start_passes_the_text_as_command_words_and_answers_early(
        self,
    ) -> None:
        seen: dict = {}
        release = asyncio.Event()

        async def slow_plan(update, context) -> None:
            seen["args"] = list(context.args)
            await update.message.reply_text("Planning with Claude...")
            await release.wait()

        args = {"kind": "plan", "text": "add  login page"}
        with patch.dict(self.work.STARTERS, {"plan": slow_plan}):
            result = await self.run_action("start_work", args)
            self.assertEqual(result, "Planning with Claude...")
            self.assertEqual(seen["args"], ["add", "login", "page"])
            # Still thinking: a second request must not overwrite the first.
            with self.assertRaises(self.actions.ActionError) as raised:
                await self.run_action("start_work", args)
            self.assertIn("already being planned", str(raised.exception))
            release.set()
            await asyncio.sleep(0.01)
            await self.run_action("start_work", args)
            release.set()

    async def test_start_is_refused_while_other_work_is_pending(self) -> None:
        self.data["pending_plan"] = self.plan
        with self.assertRaises(self.actions.ActionError) as raised:
            await self.run_action("start_work", {"kind": "bugfix", "text": "crash"})
        self.assertIn("pending work first", str(raised.exception))

    def test_start_text_is_one_visible_line_and_the_kind_is_fixed(self) -> None:
        def request(kind: str, text: object) -> dict:
            args = {"kind": kind, "text": text}
            return {"id": "a1b2c3d4", "action": "start_work", "args": args}

        self.inbox.validate(
            request("implement", "add a /health endpoint; keep it small")
        )
        for kind, text in (
            ("deploy", "x"),
            ("plan", ""),
            ("plan", "   "),
            ("plan", "two\nlines"),
            ("plan", "bell\x07"),
            ("plan", "x" * 4001),
            ("plan", ["x"]),
        ):
            with self.assertRaises(self.actions.ActionError, msg=repr(text)):
                self.inbox.validate(request(kind, text))

    async def test_inbox_routes_work_actions_and_validates_arguments(self) -> None:
        self.assertEqual(self.inbox.WORK_ACTIONS, self.work.WORK_ACTIONS)
        for bad in (
            {"action": "remove_queued", "args": {"task": "4; rm"}},
            {"action": "remove_queued", "args": {}},
            {"action": "approve_plan", "args": {"force": "1"}},
            {"action": "run_shell", "args": {}},
        ):
            with self.assertRaises(self.actions.ActionError):
                self.inbox.validate({"id": "a1b2c3d4", **bad})
        with self.assertRaises(self.actions.ActionError):
            await self.inbox.execute("approve_plan", {}, self.data)
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a1b2c3d4.json").write_text(
            json.dumps(
                {
                    "id": "a1b2c3d4",
                    "action": "approve_plan",
                    "args": {},
                    "requested_at": time.time(),
                }
            )
        )
        self.data["pending_plan"] = self.plan
        log = self.inbox.ResultLog(tmp / "results.json")

        async def work(action: str, args: dict) -> str:
            return await self.work.run_work_action(self.app, OWNER, action, args)

        self.assertEqual(
            await self.inbox.process_inbox(tmp, log, self.data, work=work), 1
        )
        self.assertEqual(log.results[0]["status"], "done")
        self.assertTrue(self.plan.approved)


if __name__ == "__main__":
    unittest.main()
