"""Claude Code runs on the claude.ai login: the API key stays out of its env."""

import importlib
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def claude_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("YOUR_CHAT_ID", "123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-secret")
    monkeypatch.setenv("KEEP_ME", "1")
    return importlib.import_module("ai_agent.claude_env")


def test_claude_gets_no_api_key_and_other_tools_are_untouched(claude_env):
    env = claude_env.env_for(["/usr/local/bin/claude", "-p", "x"])
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["KEEP_ME"] == "1"
    assert claude_env.env_for(["codex", "exec"]) is None
    assert claude_env.env_for([]) is None


def test_api_key_mode_inherits_the_key(claude_env, monkeypatch):
    monkeypatch.setattr(claude_env, "CLAUDE_CODE_AUTH", "api_key")
    assert claude_env.env_for(["claude", "-p"]) is None


def test_shell_runs_claude_without_the_key(claude_env, monkeypatch, tmp_path):
    shell = importlib.import_module("ai_agent.shell")
    seen = []

    def fake_run(args, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(shell.subprocess, "run", fake_run)
    shell.run(["claude", "-p", "hi"], cwd=tmp_path)
    shell.run(["git", "status"], cwd=tmp_path)
    assert "ANTHROPIC_API_KEY" not in seen[0] and seen[0]["KEEP_ME"] == "1"
    assert seen[1] is None  # git inherits the service environment as before


def test_the_limits_probe_runs_without_the_key(claude_env, monkeypatch):
    probe = importlib.import_module("ai_agent.claude_code_probe")
    seen = {}
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda argv, **kwargs: seen.update(kwargs) or SimpleNamespace(),
    )
    probe.run(probe.command())
    assert "ANTHROPIC_API_KEY" not in seen["env"]
