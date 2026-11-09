"""Provider capability blocks, role validation, model ids, and attempt snapshots.

These are the contract fixtures for the dashboard-facing snapshot: old fields
stay, capability blocks are added, unknown/disabled IDs are refused, the
claude/claude-code runner alias is kept explicit, slash model ids are accepted
without opening the door to paths or commands, and a selection change applies
only to future attempts.
"""

import asyncio
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class CapabilitiesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        self.caps = importlib.import_module("ai_agent.provider_capabilities")
        self.actions = importlib.import_module("ai_agent.actions")
        self.inbox = importlib.import_module("ai_agent.inbox")
        self.state = importlib.import_module("ai_agent.bot.state")
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)

    def request(self, action, args, request_id="a1b2c3d4") -> dict:
        return {"id": request_id, "action": action, "args": args}


# ------------------------------------------------------------------- model ids


class ModelIdTests(CapabilitiesTestCase):
    def test_slash_model_ids_are_valid(self) -> None:
        for model in (
            "claude-sonnet-4-6",
            "gpt-5-codex",
            "deepseek/deepseek-chat",
            "a/b/c",
            "x.y-z:1",
        ):
            self.assertTrue(self.caps.valid_model_id(model), model)

    def test_paths_and_commands_are_rejected(self) -> None:
        for model in (
            "a b",
            "../etc/passwd",
            "/etc/passwd",
            "a/",
            "/a",
            "a//b",
            "a..b",
            "rm -rf /",
            "a;rm",
            "|sh",
            "$(id)",
            "a\nb",
            "",
        ):
            self.assertFalse(self.caps.valid_model_id(model), model)

    def test_inbox_accepts_slash_and_rejects_paths(self) -> None:
        for model in ("deepseek/deepseek-chat", "claude-sonnet-4-6"):
            self.inbox.validate(
                self.request("switch_model", {"tool": "claude", "model": model})
            )
        for model in ("../etc", "/tmp/x", "a b", "rm -rf /"):
            with self.assertRaises(self.inbox.ActionError):
                self.inbox.validate(
                    self.request("switch_model", {"tool": "claude", "model": model})
                )


# ------------------------------------------------------------------ role lists


class RoleListTests(CapabilitiesTestCase):
    def test_enabled_role_lists_keep_legacy_order(self) -> None:
        self.assertEqual(
            self.caps.enabled_tools_for_role("planner"), ["codex", "claude", "deepseek"]
        )
        self.assertEqual(
            self.caps.enabled_tools_for_role("executor"), ["codex", "claude"]
        )
        self.assertEqual(self.caps.enabled_manageable_tools(), ["claude", "codex"])

    def test_unknown_and_disabled_ids_are_refused(self) -> None:
        self.assertEqual(self.caps.require_enabled_tool("CLAUDE", "planner"), "claude")
        self.assertEqual(
            self.caps.require_enabled_tool("DEEPSEEK", "planner"), "deepseek"
        )
        for bad in ("gpt", "opencode", "claude-code"):
            with self.assertRaises(ValueError):
                self.caps.require_enabled_tool(bad, "planner")


# ---------------------------------------------------------------- capability blocks


class CapabilityBlockTests(CapabilitiesTestCase):
    def test_blocks_publish_only_declared_capabilities(self) -> None:
        by_id = {block["id"]: block for block in self.caps.provider_blocks()}
        self.assertEqual(
            set(by_id), {"claude", "codex", "claude-code", "opencode", "deepseek"}
        )
        claude = by_id["claude"]
        self.assertEqual(claude["label"], "Claude")
        self.assertEqual(claude["status"], "enabled")
        self.assertEqual(claude["roles"], ["planner"])
        self.assertEqual(claude["runner"], "claude")
        self.assertIsInstance(claude["model"], dict)
        self.assertTrue(claude["model"]["manageable"])
        self.assertIn("planner", claude["adapter"])
        claude_code = by_id["claude-code"]
        self.assertEqual(claude_code["label"], "Claude Code")
        self.assertEqual(claude_code["runner"], "claude")
        self.assertIn("executor", claude_code["roles"])
        deepseek = by_id["deepseek"]
        self.assertEqual(deepseek["status"], "enabled")
        self.assertEqual(deepseek["roles"], ["planner"])
        self.assertIsInstance(deepseek["model"], dict)
        self.assertFalse(deepseek["model"]["manageable"])
        self.assertIn("planner", deepseek["adapter"])
        self.assertEqual(deepseek["usage"], "declared")
        self.assertIs(deepseek["images"], False)
        self.assertIs(deepseek["structured_output"], True)

    def test_blocks_are_json_serializable(self) -> None:
        json.dumps(self.caps.provider_blocks())

    def test_resolved_model_prefers_persisted_override(self) -> None:
        data = {"planner_model": "deepseek/deepseek-chat"}
        self.assertEqual(
            self.caps.resolved_model("claude", "planner", data),
            "deepseek/deepseek-chat",
        )
        tool = SimpleNamespace(current_model=lambda: "default-model")
        with patch.object(self.caps, "get_tool", return_value=tool):
            self.assertEqual(
                self.caps.resolved_model("x", "executor", {}), "default-model"
            )


# ------------------------------------------------------------------- setup view


class SnapshotContractTests(CapabilitiesTestCase):
    def test_state_part_keeps_legacy_fields_and_adds_models(self) -> None:
        view = importlib.import_module("ai_agent.bot.setup_view")
        part = view.state_part({"planning_agent": "claude"})
        self.assertEqual(part["planner"], "claude")
        self.assertEqual(part["planner_options"], ["codex", "claude", "deepseek"])
        self.assertEqual(part["implementer_options"], ["codex", "claude"])
        self.assertIn("planner_model", part)
        self.assertIn("implementer_model", part)
        self.assertIn("busy", part)

    def test_real_registry_setup_survives_disabled_opencode(self) -> None:
        view = importlib.import_module("ai_agent.bot.setup_view")
        from ai_agent.ai_tools import all_info
        from ai_agent.providers import opencode
        from ai_agent.tool_auth import ToolAuth

        with (
            patch.object(opencode, "enabled", False),
            patch.object(view, "list_projects", return_value=[]),
            patch.object(
                view, "active_project", return_value=SimpleNamespace(name="a")
            ),
        ):
            self.assertIn("opencode", [info.tool for info in all_info()])
            auth = ToolAuth().snapshot()
            self.assertNotIn("opencode", auth)
            part = view.files_part(view.ModelChoices(), self.tmp / "missing", auth)
        self.assertNotIn("opencode", [model["tool"] for model in part["models"]])
        self.assertIn("codex", [model["tool"] for model in part["models"]])

    def test_files_part_publishes_capabilities(self) -> None:
        view = importlib.import_module("ai_agent.bot.setup_view")
        results = self.tmp / "results.json"
        results.write_text("[]")
        with (
            patch.object(view, "list_projects", return_value=[]),
            patch.object(
                view, "active_project", return_value=SimpleNamespace(name="a")
            ),
            patch.object(view, "all_info", return_value=[]),
        ):
            part = view.files_part(view.ModelChoices(), results)
        self.assertTrue(part["capabilities"])
        self.assertIn("projects", part)
        self.assertIn("models", part)
        self.assertIn("actions", part)


# ------------------------------------------------------------------ action core


class ActionValidationTests(CapabilitiesTestCase):
    def test_set_planner_and_implementer_persist_independently(self) -> None:
        state = {}
        self.actions.set_planner("claude", state)
        self.actions.set_implementer("codex", state)
        self.assertEqual(state["planning_agent"], "claude")
        self.assertEqual(state["implementation_agent"], "codex")

    def test_changing_provider_drops_its_old_model_override(self) -> None:
        state = {"planner_model": "old-model"}
        self.actions.set_planner("codex", state)
        self.assertNotIn("planner_model", state)

    def test_set_planner_refuses_unknown_and_disabled(self) -> None:
        state = {}
        for bad in ("gpt", "opencode"):
            with self.assertRaises(self.actions.ActionError):
                self.actions.set_planner(bad, state)
        self.assertEqual(state, {})

    def test_switch_model_rejects_invalid_model_before_verify(self) -> None:
        tool = SimpleNamespace(name="claude", manageable=True, verify=MagicMock())
        with patch.object(self.actions, "get_tool", return_value=tool):
            with self.assertRaisesRegex(self.actions.ActionError, "not a valid model"):
                asyncio.run(self.actions.switch_model("claude", "a b", {}))
        tool.verify.assert_not_called()

    def test_switch_model_persists_role_models(self) -> None:
        tool = SimpleNamespace(
            name="claude",
            manageable=True,
            roles=("planner", "executor"),
            verify=lambda m: (True, "ok"),
            set_model=MagicMock(),
        )
        state = {}
        with (
            patch.object(self.actions, "get_tool", return_value=tool),
            patch.object(self.actions, "schedule_restart", return_value="Restarting."),
        ):
            asyncio.run(
                self.actions.switch_model("claude", "deepseek/deepseek-chat", state)
            )
        self.assertEqual(state["planner_model"], "deepseek/deepseek-chat")
        self.assertEqual(state["implementer_model"], "deepseek/deepseek-chat")
        tool.set_model.assert_called_once_with("deepseek/deepseek-chat")


# ------------------------------------------------------------- future-attempt apply


class FutureAttemptTests(CapabilitiesTestCase):
    def test_enqueue_records_resolved_identity(self) -> None:
        state = {}
        queued = self.state.enqueue_pending_implementation(
            state,
            {
                "change": "x",
                "images": [],
                "codex_prompt": "p",
                "branch_name": "feature/x",
                "implementation_agent": "codex",
            },
        )
        self.assertEqual(queued["implementation_agent"], "codex")
        self.assertTrue(queued["implementation_model"])
        self.assertEqual(
            state["task_queue"][0]["implementation_model"],
            queued["implementation_model"],
        )

    def test_retry_re_resolves_model_from_current_selection(self) -> None:
        state = {"implementer_model": "fresh-model"}
        queued = self.state.enqueue_pending_implementation(
            state,
            {
                "change": "x",
                "images": [],
                "codex_prompt": "p",
                "branch_name": "feature/x",
                "implementation_agent": "codex",
                "implementation_model": "stale-model",
                "checkpoint_task": "t1",
            },
        )
        self.assertEqual(queued["implementation_model"], "fresh-model")

    def test_snapshot_queue_carries_recorded_model(self) -> None:
        snap = self.state.snapshot(
            {
                "task_queue": [
                    {
                        "id": 1,
                        "branch_name": "feature/x",
                        "implementation_agent": "codex",
                        "implementation_model": "m",
                    }
                ]
            }
        )
        self.assertEqual(snap["queue"][0]["model"], "m")


# ---------------------------------------------------------------------- inbox ids


class InboxIdTests(CapabilitiesTestCase):
    def test_retry_and_resume_tool_ids_are_registry_validated(self) -> None:
        for action in ("retry_task", "resume_task"):
            for tool in ("claude", "codex"):
                self.inbox.validate(
                    self.request(action, {"task": "abcd1234", "tool": tool})
                )
            with self.assertRaises(self.inbox.ActionError):
                self.inbox.validate(
                    self.request(action, {"task": "abcd1234", "tool": "gpt"})
                )


if __name__ == "__main__":
    unittest.main()
