"""Dashboard setup requests: the shared action core and the agent's inbox."""

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


class InboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        self.actions = importlib.import_module("ai_agent.actions")
        self.inbox = importlib.import_module("ai_agent.inbox")
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    def request(self, action, args, request_id="a1b2c3d4", age=0.0) -> dict:
        return {
            "id": request_id,
            "action": action,
            "args": args,
            "requested_at": time.time() - age,
        }


# ---------------------------------------------------------------- action core


class ActionCoreTests(InboxTestCase):
    def test_busy_reason(self) -> None:
        busy = self.actions.busy_reason
        self.assertIsNone(busy({}))
        self.assertEqual(
            busy({"active_execution": {"branch": "x"}}), "a task is running"
        )
        self.assertEqual(busy({"task_queue": [{}, {}]}), "2 task(s) are queued")

    def test_use_project_refused_while_busy(self) -> None:
        with patch.object(self.actions, "set_active") as set_active:
            with self.assertRaisesRegex(
                self.actions.ActionError, "while a task is running"
            ):
                asyncio.run(
                    self.actions.use_project("x", {"active_execution": {"branch": "b"}})
                )
        set_active.assert_not_called()

    def test_use_project_when_idle(self) -> None:
        project = SimpleNamespace(name="cc", github_repository="o/cc")
        with patch.object(
            self.actions, "set_active", return_value=project
        ) as set_active:
            self.assertIs(asyncio.run(self.actions.use_project("cc", {})), project)
        set_active.assert_called_once_with("cc")

    def test_planner_and_implementer(self) -> None:
        state = {}
        self.assertIn("Claude", self.actions.set_planner("claude", state))
        self.assertIn("Codex", self.actions.set_implementer("codex", state))
        self.assertEqual(
            state, {"planning_agent": "claude", "implementation_agent": "codex"}
        )
        with self.assertRaises(self.actions.ActionError):
            self.actions.set_planner("gpt", state)

    def test_switch_model_refuses_read_only_and_unreachable(self) -> None:
        read_only = SimpleNamespace(
            name="codex",
            manageable=False,
            info=lambda: SimpleNamespace(note="set in Codex config"),
        )
        with patch.object(self.actions, "get_tool", return_value=read_only):
            with self.assertRaisesRegex(self.actions.ActionError, "read-only"):
                asyncio.run(self.actions.switch_model("codex", "m"))
        bad = SimpleNamespace(
            name="claude",
            manageable=True,
            verify=lambda m: (False, "not found"),
            set_model=MagicMock(),
        )
        with (
            patch.object(self.actions, "get_tool", return_value=bad),
            patch.object(self.actions, "schedule_restart") as restart,
        ):
            with self.assertRaisesRegex(self.actions.ActionError, "unchanged"):
                asyncio.run(self.actions.switch_model("claude", "nope"))
        bad.set_model.assert_not_called()
        restart.assert_not_called()

    def test_switch_model_verifies_saves_and_schedules_restart(self) -> None:
        good = SimpleNamespace(
            name="claude",
            manageable=True,
            verify=lambda m: (True, "ok"),
            set_model=MagicMock(),
        )
        with (
            patch.object(self.actions, "get_tool", return_value=good),
            patch.object(
                self.actions, "schedule_restart", return_value="Restarting."
            ) as restart,
        ):
            message = asyncio.run(self.actions.switch_model("claude", "claude-opus-x"))
        good.set_model.assert_called_once_with("claude-opus-x")
        restart.assert_called_once()
        self.assertIn("Verified and saved claude model = claude-opus-x", message)

    def test_add_repository_reports_failed_clone(self) -> None:
        project = SimpleNamespace(
            name="r", github_repository="o/r", repo_path=self.tmp / "p" / "r"
        )
        with (
            patch.object(self.actions, "add_project", return_value=(project, True)),
            patch.object(
                self.actions,
                "run",
                side_effect=RuntimeError("Permission denied (publickey)"),
            ),
        ):
            with self.assertRaisesRegex(
                self.actions.ActionError, "registered, but cloning failed"
            ):
                asyncio.run(self.actions.add_repository("o/r"))


# ---------------------------------------------------------------- validation


class ValidationTests(InboxTestCase):
    def test_accepts_each_known_action(self) -> None:
        for action, args in [
            ("use_project", {"name": "channel-cast"}),
            ("add_repository", {"repository": "ramunl/ai-dashboard"}),
            ("set_planner", {"value": "claude"}),
            ("set_implementer", {"value": "codex"}),
            ("switch_model", {"tool": "claude", "model": "claude-sonnet-4-6"}),
            ("switch_model", {"tool": "codex", "model": "default"}),
        ]:
            self.assertEqual(self.inbox.validate(self.request(action, args))[1], action)

    def test_refuses_anything_else(self) -> None:
        bad = [
            self.request("run_shell", {"cmd": "rm -rf /"}),
            self.request("use_project", {"name": "../../etc"}),
            self.request("use_project", {"name": "cc", "extra": "x"}),
            self.request("add_repository", {"repository": "owner/repo; rm -rf /"}),
            self.request("add_repository", {"repository": "https://evil.example/x/y"}),
            self.request("set_planner", {"value": "gpt"}),
            self.request("switch_model", {"tool": "claude-code", "model": "m"}),
            self.request("switch_model", {"tool": "claude", "model": "a b"}),
            self.request("use_project", {"name": "cc"}, request_id="../../x"),
            self.request("use_project", "cc"),
        ]
        for request in bad:
            with self.assertRaises(self.inbox.ActionError, msg=str(request)):
                self.inbox.validate(request)


# ---------------------------------------------------------------- processing


class ProcessTests(InboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = self.tmp / "inbox"
        self.dir.mkdir()
        self.log = self.inbox.ResultLog(self.tmp / "results.json")

    def drop(self, request: dict, name: str | None = None) -> Path:
        path = self.dir / f"{name or request['id']}.json"
        path.write_text(json.dumps(request))
        return path

    def results(self) -> dict:
        return {r["id"]: r for r in json.loads((self.tmp / "results.json").read_text())}

    def test_runs_request_records_result_and_removes_file(self) -> None:
        state = {}
        self.drop(self.request("set_planner", {"value": "claude"}))
        handled = asyncio.run(self.inbox.process_inbox(self.dir, self.log, state))
        self.assertEqual(handled, 1)
        self.assertEqual(state["planning_agent"], "claude")
        self.assertEqual(self.results()["a1b2c3d4"]["status"], "done")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_old_request_expires_instead_of_running(self) -> None:
        state = {}
        self.drop(self.request("set_planner", {"value": "claude"}, age=600))
        asyncio.run(self.inbox.process_inbox(self.dir, self.log, state))
        self.assertEqual(state, {})
        self.assertEqual(self.results()["a1b2c3d4"]["status"], "expired")

    def test_busy_switch_fails_with_reason(self) -> None:
        state = {"task_queue": [{}]}
        self.drop(self.request("use_project", {"name": "cc"}))
        asyncio.run(self.inbox.process_inbox(self.dir, self.log, state))
        result = self.results()["a1b2c3d4"]
        self.assertEqual(result["status"], "failed")
        self.assertIn("1 task(s) are queued", result["message"])

    def test_bad_requests_are_rejected_and_do_not_block_the_next(self) -> None:
        (self.dir / "garbage.json").write_text("{ nope")
        self.drop(self.request("run_shell", {"cmd": "id"}, request_id="0000aaaa"))
        self.drop(
            self.request("set_implementer", {"value": "claude"}, request_id="1111bbbb")
        )
        state = {}
        asyncio.run(self.inbox.process_inbox(self.dir, self.log, state))
        results = self.results()
        self.assertEqual(results["garbage"]["status"], "rejected")
        self.assertEqual(results["0000aaaa"]["status"], "rejected")
        self.assertEqual(results["1111bbbb"]["status"], "done")
        self.assertEqual(state["implementation_agent"], "claude")

    def test_unexpected_error_is_recorded(self) -> None:
        self.drop(self.request("set_planner", {"value": "claude"}))
        with patch.object(self.inbox, "set_planner", side_effect=KeyError("boom")):
            asyncio.run(self.inbox.process_inbox(self.dir, self.log, {}))
        self.assertIn("Unexpected error", self.results()["a1b2c3d4"]["message"])

    def test_results_survive_and_stay_bounded(self) -> None:
        for index in range(25):
            self.log.record(f"{index:08x}", "set_planner", "done", "ok")
        reloaded = self.inbox.ResultLog(self.tmp / "results.json")
        self.assertEqual(len(reloaded.results), self.inbox.KEEP_RESULTS)
        self.assertEqual(reloaded.results[0]["id"], f"{24:08x}")


class InboxLoopTests(InboxTestCase):
    def test_handled_requests_are_marked_for_persistence(self) -> None:
        inbox_dir = self.tmp / "inbox"
        inbox_dir.mkdir()
        (inbox_dir / "a1b2c3d4.json").write_text(
            json.dumps(self.request("set_planner", {"value": "claude"}))
        )
        app = SimpleNamespace(
            user_data={123: {}},
            bot=SimpleNamespace(set_my_name=AsyncMock()),
            mark_data_for_update_persistence=MagicMock(),
        )

        async def run_once() -> None:
            with patch.object(self.inbox, "POLL_SECONDS", 0.01):
                task = asyncio.create_task(
                    self.inbox.inbox_forever(
                        app, 123, inbox_dir, self.tmp / "results.json"
                    )
                )
                await asyncio.sleep(0.1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(run_once())
        self.assertEqual(app.user_data[123]["planning_agent"], "claude")
        app.mark_data_for_update_persistence.assert_called_with(user_ids=123)


if __name__ == "__main__":
    unittest.main()


class SetupViewTests(InboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.view = importlib.import_module("ai_agent.bot.setup_view")

    def test_state_part(self) -> None:
        part = self.view.state_part({"planning_agent": "claude", "task_queue": [{}]})
        self.assertEqual(part["planner"], "claude")
        self.assertEqual(part["planner_options"], ["codex", "claude"])
        self.assertEqual(part["busy"], "1 task(s) are queued")

    def test_files_part_lists_projects_models_and_results(self) -> None:
        projects = [
            SimpleNamespace(name="a", github_repository="o/a"),
            SimpleNamespace(name="b", github_repository="o/b"),
        ]
        info = [
            SimpleNamespace(
                tool="claude", model="claude-sonnet-4-6", manageable=True, note=""
            ),
            SimpleNamespace(
                tool="codex",
                model="gpt-5-codex",
                manageable=False,
                note="set in Codex config",
            ),
        ]
        results = self.tmp / "results.json"
        results.write_text(json.dumps([{"id": "a1b2c3d4", "status": "done"}]))
        choices = self.view.ModelChoices()
        choices.choices["claude"] = ["claude-sonnet-4-6", "claude-opus-x"]
        with (
            patch.object(self.view, "list_projects", return_value=projects),
            patch.object(self.view, "active_project", return_value=projects[1]),
            patch.object(self.view, "all_info", return_value=info),
        ):
            part = self.view.files_part(choices, results)
        self.assertEqual(
            [(p["name"], p["active"]) for p in part["projects"]],
            [("a", False), ("b", True)],
        )
        self.assertEqual(
            part["models"][0]["choices"], ["claude-sonnet-4-6", "claude-opus-x"]
        )
        self.assertEqual(part["models"][1]["choices"], [])
        self.assertEqual(part["actions"][0]["status"], "done")

    def test_model_choices_keep_last_good_list_on_error(self) -> None:
        info = [SimpleNamespace(tool="claude", manageable=True)]
        tool = SimpleNamespace(list_models=lambda: (True, [{"id": "m1"}, {"id": "m2"}]))
        choices = self.view.ModelChoices()
        with (
            patch.object(self.view, "all_info", return_value=info),
            patch.object(self.view, "get_tool", return_value=tool),
        ):
            choices.refresh()
            tool.list_models = lambda: (False, "HTTP 401")
            choices.refresh()
        self.assertEqual(choices.choices["claude"], ["m1", "m2"])
        self.assertEqual(choices.errors["claude"], "HTTP 401")
