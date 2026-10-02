"""Behavior tests for bot maintenance."""

import asyncio
import importlib
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.bot_fixtures import TelegramTestCase


class MaintenanceTests(TelegramTestCase):
    def test_version_reports_shared_runtime_version(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.maintenance")
        message = types.SimpleNamespace(reply_text=AsyncMock())
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=[], user_data={})

        with patch.object(
            telegram_bot, "get_runtime_version", return_value="ai-coding-agent v9"
        ) as runtime:
            asyncio.run(telegram_bot.version(update, context))

        runtime.assert_called_once_with()
        message.reply_text.assert_awaited_once_with("ai-coding-agent v9")

    def test_core_update_target_bumps_and_deploys_selected_bot(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.maintenance")

        with (
            patch.object(
                telegram_bot,
                "bump_core_isolated",
                return_value=(True, "Core bumped to v2.0 and pushed."),
            ) as bump,
            patch.object(
                telegram_bot,
                "_run_target_deploy",
                return_value="Deployed ai-pm-agent.",
            ) as deploy,
        ):
            result = telegram_bot._core_update_target("pm")

        repo = telegram_bot.DEPLOY_TARGETS["pm"]["repo"]
        bump.assert_called_once_with(repo)
        deploy.assert_called_once_with(telegram_bot.DEPLOY_TARGETS["pm"])
        self.assertIn("Core bumped to v2.0", result)
        self.assertIn("Deployed ai-pm-agent", result)

    def test_core_update_target_rejects_unknown_bot(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.maintenance")

        result = telegram_bot._core_update_target("unknown")

        self.assertIn("Unknown bot 'unknown'", result)
        self.assertIn("coding, ops, pm", result)

    def test_deploy_without_branch_shows_usage(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.maintenance")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=[], user_data={})

        with patch.object(telegram_bot, "run") as mock_run:
            asyncio.run(telegram_bot.deploy(update, context))

        mock_run.assert_not_called()
        self.assertIn("Usage: /deploy", message.replies[0])

    def test_deploy_rejects_more_than_one_argument(self) -> None:
        telegram_bot = importlib.import_module("ai_agent.bot.maintenance")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=["ops", "feature-x"], user_data={})

        with patch.object(telegram_bot, "run") as mock_run:
            asyncio.run(telegram_bot.deploy(update, context))

        mock_run.assert_not_called()
        self.assertIn("Usage: /deploy <branch>", message.replies[0])

    def test_self_deploy_only_submits_independent_job(self) -> None:
        maintenance = importlib.import_module("ai_agent.bot.maintenance")
        message = types.SimpleNamespace(reply_text=AsyncMock())
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        with (
            patch.object(
                maintenance,
                "active_project",
                return_value=types.SimpleNamespace(
                    name="ai-coding-agent", github_repository="ramunl/ai-coding-agent"
                ),
            ),
            patch.object(
                maintenance,
                "run",
                return_value=types.SimpleNamespace(
                    output='{"status":"queued","operation":"op1"}'
                ),
            ) as command,
        ):
            asyncio.run(
                maintenance.deploy(update, types.SimpleNamespace(args=["main"]))
            )
        command.assert_called_once_with(
            [
                "/usr/local/sbin/ai-deploy",
                "submit",
                "deploy",
                "ai-coding-agent",
                "main",
            ],
            cwd=Path("/opt"),
            timeout=30,
        )
        self.assertIn("op1", message.reply_text.await_args.args[0])

    def test_failed_queue_does_not_restart_or_read_logs(self) -> None:
        maintenance = importlib.import_module("ai_agent.bot.maintenance")
        message = types.SimpleNamespace(reply_text=AsyncMock())
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        with (
            patch.object(
                maintenance,
                "active_project",
                return_value=types.SimpleNamespace(
                    name="ai-coding-agent", github_repository="ramunl/ai-coding-agent"
                ),
            ),
            patch.object(
                maintenance, "run", side_effect=RuntimeError("busy")
            ) as command,
        ):
            asyncio.run(
                maintenance.deploy(update, types.SimpleNamespace(args=["main"]))
            )
        self.assertEqual(command.call_count, 1)
        self.assertIn("Deploy failed: busy", message.reply_text.await_args.args[0])

    def test_core_deploy_uses_same_queued_manager(self) -> None:
        maintenance = importlib.import_module("ai_agent.bot.maintenance")
        with patch.object(
            maintenance,
            "run",
            return_value=types.SimpleNamespace(
                output='{"status":"queued","operation":"core1"}'
            ),
        ) as command:
            result = maintenance._run_target_deploy(maintenance.DEPLOY_TARGETS["pm"])
        self.assertIn("core1", result)
        self.assertEqual(
            command.call_args.args[0],
            ["/usr/local/sbin/ai-deploy", "submit", "deploy", "ai-pm-agent", "main"],
        )


class DeployTargetPathTests(unittest.TestCase):
    """The self-deploy target must match the script this repo ships.

    A rename once left /deploy calling /usr/local/sbin/update-ai-agent after
    the script became update-ai-coding-agent; tests pinned the stale path.
    """

    def test_self_deploy_matches_shipped_script_and_log(self) -> None:
        from ai_agent.bot.constants import DEPLOY_TARGETS

        repo = Path(__file__).resolve().parent.parent
        target = DEPLOY_TARGETS["coding"]
        script_name = Path(target["script"]).name
        shipped = repo / "deploy" / script_name
        self.assertTrue(shipped.is_file(), f"deploy/{script_name} not in repo")


class DashboardDeployTests(TelegramTestCase):
    """The dashboard is deployable like the agents, but takes no core updates."""

    def _deploy(self, project_name: str, repository: str) -> tuple[list, list]:
        maintenance = importlib.import_module("ai_agent.bot.maintenance")

        class FakeMessage:
            def __init__(self) -> None:
                self.replies = []

            async def reply_text(self, text: str) -> None:
                self.replies.append(text)

        message = FakeMessage()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123), message=message
        )
        context = types.SimpleNamespace(args=["main"], user_data={})
        calls = []

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            calls.append(args)
            return types.SimpleNamespace(output='{"status":"queued","operation":"op2"}')

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(maintenance, "run", fake_run),
            patch.object(maintenance.asyncio, "to_thread", side_effect=fake_to_thread),
            patch.object(
                maintenance,
                "active_project",
                return_value=types.SimpleNamespace(
                    name=project_name, github_repository=repository
                ),
            ),
        ):
            asyncio.run(maintenance.deploy(update, context))
        return calls, message.replies

    def test_deploy_dashboard_runs_its_script(self) -> None:
        calls, replies = self._deploy("ai-dashboard", "ramunl/ai-dashboard")
        self.assertEqual(
            calls,
            [["/usr/local/sbin/ai-deploy", "submit", "deploy", "ai-dashboard", "main"]],
        )
        self.assertIn("Deploy queued", replies[-1])

    def test_dashboard_found_by_repository_name(self) -> None:
        calls, _ = self._deploy("my-dash", "ramunl/ai-dashboard")
        self.assertEqual(calls[0][3], "ai-dashboard")

    def test_not_deployable_message_lists_every_target(self) -> None:
        calls, replies = self._deploy(
            "channel-cast", "ramunl/com.randrgames.channelcast"
        )
        self.assertEqual(calls, [])
        for name in ("ai-coding-agent", "ai-dashboard", "ai-ops-agent", "ai-pm-agent"):
            self.assertIn(name, replies[0])

    def test_core_update_does_not_offer_or_accept_the_dashboard(self) -> None:
        maintenance = importlib.import_module("ai_agent.bot.maintenance")
        from ai_agent.bot.constants import CORE_TARGETS

        self.assertEqual(sorted(CORE_TARGETS), ["coding", "ops", "pm"])
        self.assertIn(
            "Unknown bot 'dashboard'", maintenance._core_update_target("dashboard")
        )


class MissingManagerTests(TelegramTestCase):
    def test_missing_manager_explains_installation(self) -> None:
        maintenance = importlib.import_module("ai_agent.bot.maintenance")
        with patch.object(maintenance, "run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                maintenance._submit_deployment(maintenance.DEPLOY_TARGETS["pm"], "main")
