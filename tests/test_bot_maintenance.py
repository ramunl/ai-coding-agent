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
                "bump_to_latest",
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
        bump.assert_called_once_with(repo / "ai_agent_common", repo, "ai_agent_common")
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

    def test_deploy_ops_runs_script_with_branch_and_does_not_self_restart(self) -> None:
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
        context = types.SimpleNamespace(args=["feature-x"], user_data={})

        calls = []

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            calls.append(args)
            return types.SimpleNamespace(output="update finished")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(telegram_bot, "run", fake_run),
            patch.object(telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread),
            patch.object(
                telegram_bot,
                "active_project",
                return_value=types.SimpleNamespace(
                    name="ai-ops-agent", github_repository="ramunl/ai-ops-agent"
                ),
            ),
            patch.object(telegram_bot, "schedule_restart") as mock_restart,
        ):
            asyncio.run(telegram_bot.deploy(update, context))

        self.assertEqual(calls[0], ["/usr/local/sbin/update-ai-ops-agent", "feature-x"])
        mock_restart.assert_not_called()
        self.assertIn("Deploying 'feature-x' to ai-ops-agent", message.replies[0])
        self.assertIn("Deploy finished", message.replies[-1])

    def test_deploy_coding_self_deploy_skips_script_restart_and_schedules_detached_restart(
        self,
    ) -> None:
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
        context = types.SimpleNamespace(args=["main"], user_data={})

        calls = []

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            calls.append(args)
            return types.SimpleNamespace(output="update finished")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(telegram_bot, "run", fake_run),
            patch.object(telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread),
            patch.object(
                telegram_bot,
                "active_project",
                return_value=types.SimpleNamespace(
                    name="ai-coding-agent", github_repository="ramunl/ai-coding-agent"
                ),
            ),
            patch.object(
                telegram_bot,
                "schedule_restart",
                return_value="Restart scheduled in 3s.",
            ) as mock_restart,
        ):
            asyncio.run(telegram_bot.deploy(update, context))

        self.assertEqual(
            calls[0], ["/usr/local/sbin/update-ai-coding-agent", "main", "--no-restart"]
        )
        mock_restart.assert_called_once()
        self.assertIn("Restart scheduled in 3s.", message.replies[-1])

    def test_deploy_self_deploy_failure_does_not_trigger_restart(self) -> None:
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
        context = types.SimpleNamespace(args=["main"], user_data={})

        def fake_run(args, cwd=None, timeout=None, interactive=False):
            if args[0] == "/usr/local/sbin/update-ai-coding-agent":
                raise RuntimeError(
                    "Command failed (1): update-ai-coding-agent\nconflict"
                )
            return types.SimpleNamespace(output="update failed log tail")

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with (
            patch.object(telegram_bot, "run", fake_run),
            patch.object(telegram_bot.asyncio, "to_thread", side_effect=fake_to_thread),
            patch.object(
                telegram_bot,
                "active_project",
                return_value=types.SimpleNamespace(
                    name="ai-coding-agent", github_repository="ramunl/ai-coding-agent"
                ),
            ),
            patch.object(telegram_bot, "schedule_restart") as mock_restart,
        ):
            asyncio.run(telegram_bot.deploy(update, context))

        mock_restart.assert_not_called()
        self.assertIn("Deploy failed", message.replies[-1])


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
        self.assertIn(str(target["log"]), shipped.read_text())


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
            return types.SimpleNamespace(output="update finished")

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
            patch.object(maintenance, "schedule_restart") as mock_restart,
        ):
            asyncio.run(maintenance.deploy(update, context))
        mock_restart.assert_not_called()
        return calls, message.replies

    def test_deploy_dashboard_runs_its_script(self) -> None:
        calls, replies = self._deploy("ai-dashboard", "ramunl/ai-dashboard")
        self.assertEqual(calls[0], ["/usr/local/sbin/update-ai-dashboard", "main"])
        self.assertEqual(calls[1][-1], "/var/log/ai-dashboard/update.log")
        self.assertIn("Deploying 'main' to ai-dashboard", replies[0])
        self.assertIn("Deploy finished", replies[-1])

    def test_dashboard_found_by_repository_name(self) -> None:
        calls, _ = self._deploy("my-dash", "ramunl/ai-dashboard")
        self.assertEqual(calls[0][0], "/usr/local/sbin/update-ai-dashboard")

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
