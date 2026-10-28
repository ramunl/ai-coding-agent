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
from unittest.mock import AsyncMock, MagicMock, patch

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
            id="p1",
            feature="add login",
            revision=1,
            plan_text="Do it.",
            approved=False,
            repository_identity=plan_state.capture_repository_identity(),
        )
        self.data: dict = {}
        self.app = SimpleNamespace(
            user_data={OWNER: self.data},
            bot=SimpleNamespace(send_message=AsyncMock()),
            mark_data_for_update_persistence=MagicMock(),
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

    async def test_mismatched_image_request_cannot_delete_another_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for request_id in ("a1b2c3d4", "deadbeef"):
                upload = root / "uploads" / request_id
                upload.mkdir(parents=True)
                (upload / "screen.png").write_bytes(b"unclaimed")
            (root / "a1b2c3d4.json").write_text(
                json.dumps(
                    {
                        "id": "deadbeef",
                        "action": "start_work",
                        "requested_at": time.time(),
                        "args": {
                            "kind": "bugfix",
                            "text": "Broken",
                            "images": [{"name": "screen.png"}],
                        },
                    }
                )
            )
            log = self.inbox.ResultLog(root.parent / (root.name + "-results.json"))
            dispatch = AsyncMock()
            await self.inbox.process_inbox(root, log, self.data, work=dispatch)
            self.assertEqual(log.results[0]["status"], "rejected")
            dispatch.assert_not_awaited()
            self.assertFalse((root / "uploads" / "a1b2c3d4").exists())
            self.assertTrue((root / "uploads" / "deadbeef" / "screen.png").exists())
            log.path.unlink()

    async def test_image_inbox_dispatch_failure_cleans_only_unowned_images(self):
        import io

        from PIL import Image

        from ai_agent import task_images

        for retained in (False, True):
            self.data.clear()
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                inbox = root / "inbox"
                upload = inbox / "uploads" / "a1b2c3d4"
                upload.mkdir(parents=True)
                image = io.BytesIO()
                Image.new("RGB", (2, 2)).save(image, format="PNG")
                (upload / "screen.png").write_bytes(image.getvalue())
                (inbox / "a1b2c3d4.json").write_text(
                    json.dumps(
                        {
                            "id": "a1b2c3d4",
                            "action": "start_work",
                            "requested_at": time.time(),
                            "args": {
                                "kind": "bugfix",
                                "text": "Broken",
                                "images": [{"name": "screen.png"}],
                            },
                        }
                    )
                )
                seen = []

                async def fail(update, context):
                    seen.extend(update.message.images)
                    if retained:
                        context.user_data["pending_bugfix_clarification"] = {
                            "images": seen,
                            "bug": "Broken",
                        }
                    raise RuntimeError("dispatch failed")

                async def dispatch(action, args):
                    return await self.run_action(action, args)

                log = self.inbox.ResultLog(root / "results.json")
                with (
                    patch.object(task_images, "IMAGE_DIR", root / "images"),
                    patch.dict(self.work.STARTERS, {"bugfix": fail}),
                    self.assertLogs("ai_agent.bot.work_actions", "ERROR"),
                ):
                    await self.inbox.process_inbox(inbox, log, self.data, work=dispatch)
                self.assertEqual(log.results[0]["status"], "failed")
                self.assertIn("dispatch failed", log.results[0]["message"])
                self.assertEqual(len(seen), 1)
                self.assertEqual((root / "images" / seen[0]["id"]).exists(), retained)
                self.assertFalse(upload.exists())

    async def test_bug_images_survive_clarification_restart_and_confirmation(self):
        planning = importlib.import_module("ai_agent.bot.planning")
        delivery = importlib.import_module("ai_agent.image_delivery")
        persistence = importlib.import_module("ai_agent.bot.persistence")
        storage = importlib.import_module("ai_agent.task_images")
        state = importlib.import_module("ai_agent.bot.state")
        execution = importlib.import_module("ai_agent.bot.execution")
        refs = [{"id": "00000000000000000001-" + "a" * 32 + ".png"}]
        seen = []

        def assess(*args):
            seen.append(delivery.attachments())
            return "Which screen?" if len(seen) < 3 else None

        async def finish():
            await asyncio.gather(*list(self.work._runs))
            await asyncio.sleep(0)

        with (
            patch.object(planning, "get_bugfix_questions", side_effect=assess),
            patch.object(planning, "build_bugfix_prompt", return_value="Fix it"),
            patch.object(
                delivery, "resolve", return_value=([("image/png", b"img")], 0)
            ) as resolve,
            patch.object(planning, "report_missing", new=AsyncMock()),
        ):
            await self.run_action(
                "start_work", {"kind": "bugfix", "text": "Broken", "images": refs}
            )
            await finish()
            restored = persistence.deserialize_user_data(
                persistence.serialize_user_data(self.data)
            )
            self.assertEqual(storage.references_in(restored), refs)
            self.data.update(restored)
            for _ in range(2):
                await self.run_action("answer_bugfix", {"text": "Home screen"})
                await finish()
            self.assertEqual(resolve.call_count, 3)
            for call in resolve.call_args_list:
                self.assertEqual(call.args[0], refs)
            self.assertNotIn("pending_bugfix_clarification", self.data)
            with patch.object(execution, "run_task_queue", new=AsyncMock()):
                await self.run_action("confirm_work")
                await finish()
            self.assertEqual(state.task_queue(self.data)[0]["images"], refs)
            self.app.mark_data_for_update_persistence.assert_called_with(user_ids=OWNER)

    async def test_bug_assessment_failure_keeps_images_for_retry_and_cancel(self):
        planning = importlib.import_module("ai_agent.bot.planning")
        inspection = importlib.import_module("ai_agent.bot.inspection")
        refs = [{"id": "00000000000000000001-" + "b" * 32 + ".png"}]
        release = asyncio.Event()

        async def fail(*args):
            await release.wait()
            raise RuntimeError("down")

        with (
            patch.object(planning, "think", side_effect=fail),
            patch.object(planning, "report_missing", new=AsyncMock()),
            self.assertLogs("ai_agent.bot.work_actions", "ERROR"),
        ):
            await self.run_action(
                "start_work", {"kind": "bugfix", "text": "Broken", "images": refs}
            )
            release.set()
            await asyncio.gather(*list(self.work._runs), return_exceptions=True)
            await asyncio.sleep(0)
        self.assertEqual(self.data["pending_bugfix_clarification"]["images"], refs)
        with patch.object(inspection, "remove") as remove:
            await self.run_action("cancel_pending")
            remove.assert_called_once_with(refs)

    async def test_missing_bug_images_warn_and_text_only_still_prepares(self):
        planning = importlib.import_module("ai_agent.bot.planning")
        intake = importlib.import_module("ai_agent.bot.image_intake")
        delivery = importlib.import_module("ai_agent.image_delivery")
        refs = [{"id": "00000000000000000001-" + "c" * 32 + ".png"}]
        for images in (refs, []):
            with (
                patch.object(planning, "get_bugfix_questions", return_value=None),
                patch.object(planning, "build_bugfix_prompt", return_value="Fix it"),
                patch.object(intake, "resolve", return_value=([], len(images))),
            ):
                self.app.bot.send_message.reset_mock()
                await self.run_action(
                    "start_work", {"kind": "bugfix", "text": "Broken", "images": images}
                )
                await asyncio.gather(*list(self.work._runs))
                await asyncio.sleep(0)
            self.assertEqual(self.data["pending_implementation"]["images"], images)
            self.assertEqual(delivery.WARNING in self.sent(), bool(images))
            self.data.clear()

    async def test_cancel_waits_for_image_assessment(self):
        planning = importlib.import_module("ai_agent.bot.planning")
        release = asyncio.Event()

        async def assess(*args):
            await release.wait()
            return "Which screen?"

        with patch.object(planning, "think", side_effect=assess):
            await self.run_action("start_work", {"kind": "bugfix", "text": "Broken"})
            result = await self.run_action("cancel_pending")
            self.assertIn("wait before cancelling", result)
            self.assertIn("pending_bugfix_clarification", self.data)
            release.set()
            await asyncio.gather(*list(self.work._runs))
            await asyncio.sleep(0)
        await self.run_action("cancel_pending")
        self.assertNotIn("pending_bugfix_clarification", self.data)

    async def test_inbox_planning_blocks_switch_until_cancelled(self) -> None:
        planning = importlib.import_module("ai_agent.bot.planning")
        release = asyncio.Event()

        async def slow_planner(*args):
            await release.wait()
            return "Plan"

        with patch.object(planning, "think", side_effect=slow_planner):
            await self.run_action("start_work", {"kind": "plan", "text": "Add search"})
            self.assertTrue(self.data["planning_active"])
            with self.assertRaises(self.actions.ActionError):
                await self.inbox.execute("use_project", {"name": "B"}, self.data)
            running = list(self.work._runs)
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        self.assertNotIn("planning_active", self.data)

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

    async def test_a_plan_for_other_repositories_is_not_approved_here(self) -> None:
        plan_text = json.dumps(
            {
                "summary": "drag todos",
                "repositories": [
                    {"name": "ai-pm-agent", "changes": "move"},
                    {"name": "ai-dashboard", "changes": "drag"},
                ],
            }
        )
        self.plan.plan_text = plan_text
        self.data["pending_plan"] = self.plan
        planning = importlib.import_module("ai_agent.bot.planning")
        active = SimpleNamespace(name="ai-coding-agent")
        with patch.object(planning, "active_project", return_value=active):
            await self.run_action("approve_plan")
        self.assertFalse(self.plan.approved)
        self.assertNotIn("pending_implementation", self.data)
        self.assertIn("does not change ai-coding-agent", self.sent()[-1])
        self.assertIn("/split", self.sent()[-1])

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

    async def test_discuss_revises_in_the_background_and_holds_approval(self) -> None:
        seen: dict = {}
        release = asyncio.Event()

        async def slow_discuss(update, context) -> None:
            seen["args"] = list(context.args)
            await update.message.reply_text("Revising plan with Claude...")
            await release.wait()

        with self.assertRaises(self.actions.ActionError) as raised:
            await self.run_action("discuss_plan", {"text": "smaller"})
        self.assertIn("no plan to revise", str(raised.exception))
        self.data["pending_plan"] = self.plan
        with patch.object(self.work, "discuss", slow_discuss):
            result = await self.run_action("discuss_plan", {"text": "keep it  small"})
            self.assertEqual(result, "Revising plan with Claude...")
            self.assertEqual(seen["args"], ["keep", "it", "small"])
            for action, args in (
                ("discuss_plan", {"text": "again"}),
                ("approve_plan", {}),
                ("confirm_work", {}),
            ):
                with self.assertRaises(self.actions.ActionError, msg=action):
                    await self.run_action(action, args)
            self.assertFalse(self.plan.approved)
            release.set()
            await asyncio.sleep(0.01)
        await self.run_action("approve_plan")
        self.assertTrue(self.plan.approved)

    def test_discuss_text_is_validated_like_new_work(self) -> None:
        def request(args: dict) -> dict:
            return {"id": "a1b2c3d4", "action": "discuss_plan", "args": args}

        self.inbox.validate(request({"text": "use sqlite instead"}))
        for args in ({"text": ""}, {"text": "a\nb"}, {}, {"text": "x", "kind": "plan"}):
            with self.assertRaises(self.actions.ActionError, msg=str(args)):
                self.inbox.validate(request(args))

    async def test_answer_goes_to_the_bugfix_questions_one_at_a_time(self) -> None:
        seen: dict = {}
        release = asyncio.Event()

        async def slow_answer(update, context) -> None:
            seen["args"] = list(context.args)
            await update.message.reply_text("Checking the updated bug report...")
            await release.wait()

        with self.assertRaises(self.actions.ActionError) as raised:
            await self.run_action("answer_bugfix", {"text": "on Android 14"})
        self.assertIn("no bugfix questions", str(raised.exception))
        self.data["pending_bugfix_clarification"] = {"bug": "crash", "questions": "?"}
        with patch.object(self.work, "answer", slow_answer):
            result = await self.run_action("answer_bugfix", {"text": "on Android  14"})
            self.assertEqual(result, "Checking the updated bug report...")
            self.assertEqual(seen["args"], ["on", "Android", "14"])
            with self.assertRaises(self.actions.ActionError) as raised:
                await self.run_action("answer_bugfix", {"text": "again"})
            self.assertIn("still being checked", str(raised.exception))
            release.set()
            await asyncio.sleep(0.01)

    def test_snapshot_publishes_bounded_bugfix_questions(self) -> None:
        state = importlib.import_module("ai_agent.bot.state")
        self.assertIsNone(state.snapshot({})["bugfix_questions"])
        # Saved by an agent that predates the questions field: nothing to show.
        old = {"pending_bugfix_clarification": {"bug": "crash"}}
        self.assertIsNone(state.snapshot(old)["bugfix_questions"])
        pending = {"bug": "crash " * 1000, "questions": "1. Which screen?"}
        snap = state.snapshot({"pending_bugfix_clarification": pending})
        self.assertTrue(snap["awaiting_bugfix_answer"])
        self.assertEqual(snap["bugfix_questions"]["questions"], "1. Which screen?")
        self.assertEqual(len(snap["bugfix_questions"]["bug"]), state.BUGFIX_TEXT_LENGTH)

    async def test_a_failing_background_run_is_reported_in_the_chat(self) -> None:
        async def failing_plan(update, context) -> None:
            await update.message.reply_text("Planning with Claude...")
            raise RuntimeError(
                "Anthropic API error 529: overloaded\n" + "x\n" * 40 + "the real cause"
            )

        with (
            patch.dict(self.work.STARTERS, {"plan": failing_plan}),
            self.assertLogs("ai_agent.bot.work_actions", "ERROR"),
        ):
            with self.assertRaises(self.actions.ActionError):
                await self.run_action(
                    "start_work", {"kind": "plan", "text": "add login"}
                )
            await asyncio.sleep(0.02)
        failure = self.sent()[-1]
        self.assertTrue(
            failure.startswith("Failed: Anthropic API error 529: overloaded")
        )
        self.assertEqual(len(failure.splitlines()), self.work.ERROR_LINES)
        # The first line says what failed; the last lines say why.
        self.assertEqual(failure.splitlines()[1], "…")
        self.assertTrue(failure.endswith("the real cause"))
        self.assertEqual(self.work._starting, set())  # a new request may start

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
        tasks = importlib.import_module("ai_agent.bot.task_actions")
        workers = importlib.import_module("ai_agent.bot.worker_commands")
        self.assertEqual(
            self.inbox.WORK_ACTIONS,
            self.work.WORK_ACTIONS + tasks.TASK_ACTIONS + workers.WORKER_ACTIONS,
        )
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


class ThinkingMarkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = str(OWNER)
        self.state = importlib.import_module("ai_agent.bot.state")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    async def test_mark_is_published_while_the_call_runs_then_cleared(self) -> None:
        data: dict = {}
        seen: list = []

        def slow_call(value: str) -> str:
            seen.append(self.state.snapshot(data)["thinking"])
            return value.upper()

        result = await self.state.think(
            data, "plan", "add login " * 50, slow_call, "ok"
        )
        self.assertEqual(result, "OK")
        self.assertEqual(seen[0]["kind"], "plan")
        self.assertEqual(len(seen[0]["about"]), self.state.THINKING_ABOUT_LENGTH)
        self.assertIsNone(self.state.snapshot(data)["thinking"])

    async def test_mark_is_cleared_when_the_call_fails_and_never_persisted(
        self,
    ) -> None:
        data: dict = {}

        def failing_call() -> None:
            raise RuntimeError("planner down")

        with self.assertRaises(RuntimeError):
            await self.state.think(data, "revise", "x", failing_call)
        self.assertNotIn("thinking", data)
        self.assertNotIn("thinking", self.state.PERSISTENT_KEYS)
