"""Deterministic preflight tests: env, PATH/HOME, probes and policy gates."""

import asyncio
import importlib
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_agent.preflight import (
    PreflightBlockedError,
    allowed_bash_commands,
    effective_claude_allowed_tools,
    preflight,
    resolve_policy,
    unapproved_verification_commands,
    verification_command_allowed,
)
from tests.bot_fixtures import TelegramTestCase


@pytest.fixture(autouse=True)
def _clear_codex_model(monkeypatch):
    monkeypatch.delenv("CODEX_MODEL", raising=False)


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "baseline.sh").write_text("#!/usr/bin/env bash\n")
    return tmp_path


def make_bin(tmp_path, *names):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return bin_dir


def make_policy(agent, checkout, bin_dir, **overrides):
    return resolve_policy(
        agent=agent,
        cwd=checkout,
        env={"PATH": str(bin_dir), "HOME": "/root"},
        isolated=overrides.get("isolated", False),
    )


def test_resolve_policy_uses_worker_home_and_strips_secrets(checkout, monkeypatch):
    monkeypatch.setattr("ai_agent.claude_env.WORKER_HOME", Path("/worker/home"))
    monkeypatch.setattr("ai_agent.preflight.WORKER_ISOLATION_ENABLED", True)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    policy = resolve_policy(agent="codex", cwd=checkout)

    assert policy.isolated is True
    assert policy.env["HOME"] == "/worker/home"
    assert "GITHUB_TOKEN" not in policy.env
    assert policy.path == policy.env["PATH"]


def test_missing_runner_on_child_path_blocks(checkout):
    bin_dir = make_bin(checkout, "pytest", "ruff")
    policy = make_policy("codex", checkout, bin_dir)

    with pytest.raises(PreflightBlockedError) as caught:
        preflight(policy=policy, probe=lambda args, _policy: True)

    message = str(caught.value)
    assert "codex" in message
    assert "Install" in message


def test_missing_ruff_blocks_preflight(checkout):
    bin_dir = make_bin(checkout, "codex", "pytest")
    policy = make_policy("codex", checkout, bin_dir)

    with pytest.raises(PreflightBlockedError) as caught:
        preflight(policy=policy, probe=lambda args, _policy: True)

    message = str(caught.value)
    assert "Install Ruff on the worker PATH" in message
    assert "ruff was not found on the child PATH" in message


def test_missing_codex_login_blocks(checkout):
    bin_dir = make_bin(checkout, "codex", "pytest", "ruff")
    policy = make_policy("codex", checkout, bin_dir)

    def probe(args, _policy):
        return tuple(args) != ("codex", "login", "status")

    with pytest.raises(PreflightBlockedError) as caught:
        preflight(policy=policy, probe=probe)

    assert "codex login status" in str(caught.value)


def test_missing_claude_config_blocks(checkout):
    bin_dir = make_bin(checkout, "claude", "pytest", "ruff")
    policy = make_policy("claude", checkout, bin_dir)

    def probe(args, _policy):
        return tuple(args) != ("claude", "config", "stat")

    with pytest.raises(PreflightBlockedError) as caught:
        preflight(policy=policy, probe=probe)

    assert "claude config stat" in str(caught.value)


def test_default_policy_approves_documented_commands(monkeypatch):
    monkeypatch.setattr("ai_agent.preflight.CLAUDE_CODE_ARGS", ())

    assert unapproved_verification_commands("claude") == ()
    assert unapproved_verification_commands("codex") == ()


def test_unsupported_command_is_not_approved():
    allowed = allowed_bash_commands(("Bash(make test:*)", "Bash(pytest:*)"))

    assert verification_command_allowed("pytest", allowed)
    assert not verification_command_allowed("ruff check", allowed)
    assert not verification_command_allowed("scripts/baseline.sh", allowed)


def test_reduced_policy_flags_missing_ruff(monkeypatch):
    monkeypatch.setattr(
        "ai_agent.preflight.CLAUDE_CODE_ALLOWED_TOOLS",
        ("Bash(pytest:*)", "Bash(scripts/baseline.sh:*)"),
    )
    monkeypatch.setattr("ai_agent.preflight.CLAUDE_CODE_ARGS", ())

    assert unapproved_verification_commands("claude") == (
        "ruff check",
        "ruff format",
    )


def test_preflight_blocks_unapproved_verification_command(checkout, monkeypatch):
    bin_dir = make_bin(checkout, "claude", "pytest", "ruff")
    policy = make_policy("claude", checkout, bin_dir)
    monkeypatch.setattr(
        "ai_agent.preflight.CLAUDE_CODE_ALLOWED_TOOLS",
        ("Bash(pytest:*)", "Bash(ruff check:*)", "Bash(ruff format:*)"),
    )
    monkeypatch.setattr("ai_agent.preflight.CLAUDE_CODE_ARGS", ())

    with pytest.raises(PreflightBlockedError) as caught:
        preflight(policy=policy, probe=lambda args, _policy: True)

    message = str(caught.value)
    assert "scripts/baseline.sh" in message
    assert "CLAUDE_CODE_ALLOWED_TOOLS" in message


def test_effective_claude_allowed_tools_uses_args_override(monkeypatch):
    monkeypatch.setattr(
        "ai_agent.preflight.CLAUDE_CODE_ARGS",
        ("--allowedTools", "Bash(make test:*)"),
    )

    assert effective_claude_allowed_tools() == ("Bash(make test:*)",)


def test_run_probe_discards_secret_output(checkout, monkeypatch):
    from ai_agent import preflight as preflight_module

    bin_dir = make_bin(checkout, "codex", "pytest", "ruff")
    policy = make_policy("codex", checkout, bin_dir)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="secret-token\n", stderr="")

    monkeypatch.setattr(preflight_module.subprocess, "run", fake_run)

    with pytest.raises(PreflightBlockedError) as caught:
        preflight(policy=policy)

    assert "secret-token" not in str(caught.value)


def test_run_implementation_agent_preflights_before_paid_work():
    from ai_agent import workflow

    blocked = PreflightBlockedError(
        action="Install Ruff on the worker PATH",
        reason="ruff was not found on the child PATH",
    )
    with (
        patch.object(workflow, "preflight", side_effect=blocked),
        patch.object(workflow, "run_with_images") as run_images,
    ):
        with pytest.raises(PreflightBlockedError):
            workflow.run_implementation_agent("fix", "codex")

    run_images.assert_not_called()


class PreflightRepairTests(TelegramTestCase):
    def setUp(self):
        super().setUp()
        # The shared ai_agent_common submodule is only checked out in CI; fake
        # just the auth entry point the bot transport needs when it is absent.
        # Assign sys.modules directly: patch.dict snapshots the whole
        # sys.modules dict and, on restore, would discard the modules imported
        # by the ai_agent.bot.execution import below (e.g. ai_agent.github).
        try:
            import ai_agent_common as core
        except ImportError:
            core = None
        self._previous_ai_agent_common = sys.modules.get("ai_agent_common")
        if core is None or not hasattr(core, "is_authorized"):
            fake_core = types.ModuleType("ai_agent_common")
            fake_core.is_authorized = lambda update, chat_id: True
            fake_core.choice_keyboard = lambda command, choices: None
            sys.modules["ai_agent_common"] = fake_core
        self.addCleanup(self._restore_ai_agent_common)
        self.execution = importlib.import_module("ai_agent.bot.execution")

    def _restore_ai_agent_common(self):
        if self._previous_ai_agent_common is None:
            sys.modules.pop("ai_agent_common", None)
        else:
            sys.modules["ai_agent_common"] = self._previous_ai_agent_common

    def test_ci_repair_preflight_block_reports_and_stops(self):
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        context = types.SimpleNamespace(user_data={})
        blocked = PreflightBlockedError(
            action="Install Ruff on the worker PATH",
            reason="ruff was not found on the child PATH",
        )
        request = self.execution._RepairRequest(
            branch_name="feature/x",
            pull_request=types.SimpleNamespace(
                number=1, url="https://example.test/pr/1", head_sha="sha"
            ),
            implementation_agent="codex",
            prompt=lambda failure: "fix",
            run=MagicMock(side_effect=blocked),
            commit_message="CI repair",
        )
        ci_result = types.SimpleNamespace(state="failed", summary="failed", url=None)

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        async def scenario():
            with (
                patch.object(self.execution, "set_active_execution"),
                patch.object(self.execution, "report_missing", AsyncMock()),
                patch.object(
                    self.execution, "build_failure_context", return_value="ctx"
                ),
                patch.object(
                    self.execution.asyncio,
                    "to_thread",
                    side_effect=fake_to_thread,
                ),
                patch.object(self.execution, "reply_chunks", AsyncMock()) as reply,
            ):
                with self.assertRaises(PreflightBlockedError):
                    await self.execution._repair_failed_ci(
                        update, context, request, ci_result
                    )
            return reply

        reply = asyncio.run(scenario())
        texts = " ".join(call.args[1] for call in reply.await_args_list)
        self.assertIn("Ops-routed blocker", texts)
        self.assertIn("Install Ruff", texts)
        request.run.assert_called_once()
