"""Context/output-control flags are version-gated, conservative and omitted."""

from ai_agent import runner_controls
from ai_agent.runner_controls import (
    CLAUDE,
    CODEX,
    context_control_args,
    supports_context_control,
)


def test_supports_unknown_runner():
    supported, reason = supports_context_control("gemini", [], "9.9.9")

    assert not supported
    assert "not a known context-control runner" in reason


def test_supports_unparseable_version():
    supported, reason = supports_context_control(CLAUDE, [], None)

    assert not supported
    assert "unverified" in reason


def test_supports_no_verified_release_by_default():
    supported, reason = supports_context_control(CLAUDE, [], "2.1.319")

    assert not supported
    assert "no claude release is verified" in reason

    supported, reason = supports_context_control(CODEX, [], "0.166.0")

    assert not supported
    assert "no codex release is verified" in reason


def test_supports_verified_release_path(monkeypatch):
    monkeypatch.setitem(runner_controls.MIN_VERSIONS, CLAUDE, (3, 0, 0))
    monkeypatch.setitem(
        runner_controls._CONTEXT_CONTROL_FLAGS, CLAUDE, ("--max-turns", "7")
    )

    supported, reason = supports_context_control(CLAUDE, [], "3.1.0")

    assert supported
    assert "available" in reason


def test_supports_older_release_rejected(monkeypatch):
    monkeypatch.setitem(runner_controls.MIN_VERSIONS, CLAUDE, (3, 0, 0))

    supported, reason = supports_context_control(CLAUDE, [], "2.9.0")

    assert not supported
    assert "predates" in reason


def test_supports_already_present_flag_rejected(monkeypatch):
    monkeypatch.setitem(runner_controls.MIN_VERSIONS, CLAUDE, (3, 0, 0))
    monkeypatch.setitem(
        runner_controls._CONTEXT_CONTROL_FLAGS, CLAUDE, ("--max-turns", "7")
    )

    supported, reason = supports_context_control(CLAUDE, ["--max-turns", "7"], "3.1.0")

    assert not supported
    assert "already enabled" in reason


def test_context_control_args_empty_by_default():
    assert context_control_args("claude") == []
    assert context_control_args("codex") == []


def test_context_control_args_omits_when_unsupported(monkeypatch):
    monkeypatch.setitem(
        runner_controls._CONTEXT_CONTROL_FLAGS, CODEX, ("--max-turns", "5")
    )
    monkeypatch.setattr(runner_controls, "probe_version", lambda runner: "0.10.0")

    assert context_control_args("codex") == []


def test_context_control_args_adds_when_verified(monkeypatch):
    monkeypatch.setitem(runner_controls.MIN_VERSIONS, CODEX, (1, 0, 0))
    monkeypatch.setitem(
        runner_controls._CONTEXT_CONTROL_FLAGS, CODEX, ("--max-turns", "5")
    )
    monkeypatch.setattr(runner_controls, "probe_version", lambda runner: "1.2.0")

    assert context_control_args("codex") == ["--max-turns", "5"]


def test_workflow_helpers_return_nothing_by_default():
    from ai_agent.workflow import (
        claude_context_control_args,
        codex_context_control_args,
    )

    assert claude_context_control_args() == []
    assert codex_context_control_args() == []


def test_implementation_command_unchanged_without_verified_flags():
    from ai_agent.workflow import implementation_command

    assert implementation_command("do work", "codex") == [
        "codex",
        "exec",
        "-s",
        "workspace-write",
        "do work",
    ]


def test_codex_command_gains_verified_flags(monkeypatch):
    monkeypatch.setitem(runner_controls.MIN_VERSIONS, CODEX, (1, 0, 0))
    monkeypatch.setitem(
        runner_controls._CONTEXT_CONTROL_FLAGS, CODEX, ("--max-turns", "5")
    )
    monkeypatch.setattr(runner_controls, "probe_version", lambda runner: "1.2.0")

    from ai_agent.ai_tools import get_tool

    command = get_tool("codex").command("do work")

    assert command == [
        "codex",
        "exec",
        "-s",
        "workspace-write",
        "--max-turns",
        "5",
        "do work",
    ]


def test_claude_command_gains_verified_flags(monkeypatch):
    monkeypatch.setitem(runner_controls.MIN_VERSIONS, CLAUDE, (3, 0, 0))
    monkeypatch.setitem(
        runner_controls._CONTEXT_CONTROL_FLAGS, CLAUDE, ("--max-turns", "7")
    )
    monkeypatch.setattr(runner_controls, "probe_version", lambda runner: "3.1.0")

    from ai_agent.ai_tools import get_tool

    command = get_tool("claude").command("do work")

    assert "--max-turns" in command
    assert command[command.index("--max-turns") + 1] == "7"
