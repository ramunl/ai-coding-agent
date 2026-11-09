"""Structured denial parsing, counting and gating are conservative and secret-free."""

import json
from types import SimpleNamespace

from ai_agent import denial_loop
from ai_agent.denial_loop import (
    DenialEvent,
    DenialLoopBlockedError,
    DenialLoopDetector,
    parse_denial_event,
    parse_version,
    supports_structured_denials,
)
from ai_agent.run_outcomes import NoCodeOutcomeError


def _claude_denial_line(operation: str = "git push origin main") -> str:
    return json.dumps(
        {
            "type": "tool_use_result",
            "tool_name": "Bash",
            "denial": {
                "denied": True,
                "tool": "Bash",
                "operation": operation,
                "policy": "disallowed_tool",
            },
        }
    )


def _codex_denial_line(operation: str = "sudo rm -rf /") -> str:
    return json.dumps(
        {
            "type": "tool_denied",
            "denied": True,
            "tool": "Bash",
            "operation": operation,
            "policy": "sandbox_denied",
        }
    )


# ---------------------------------------------------------------------- parsing


def test_claude_structured_denial_parses_with_identity() -> None:
    event = parse_denial_event(_claude_denial_line(), denial_loop.CLAUDE)
    assert event is not None
    assert event.runner == "claude"
    assert event.tool == "Bash"
    assert event.policy == "disallowed_tool"
    assert event.operation  # a fingerprint, never the raw command
    assert "git push" not in event.operation


def test_codex_structured_denial_parses_with_identity() -> None:
    event = parse_denial_event(_codex_denial_line(), denial_loop.CODEX)
    assert event is not None
    assert event.runner == "codex"
    assert event.tool == "Bash"
    assert event.policy == "sandbox_denied"
    assert event.operation
    assert "sudo" not in event.operation


def test_quoted_operation_still_yields_a_stable_fingerprint() -> None:
    raw = 'echo "quoted" && git push'
    first = parse_denial_event(_claude_denial_line(raw), denial_loop.CLAUDE)
    second = parse_denial_event(_claude_denial_line(raw), denial_loop.CLAUDE)
    assert first is not None and second is not None
    assert first.operation == second.operation
    assert first.identity() == second.identity()


def test_false_positives_are_never_denials() -> None:
    # A failing test result (is_error) is not a permission denial.
    assert (
        parse_denial_event(
            json.dumps({"type": "result", "is_error": True, "result": "tests failed"}),
            denial_loop.CLAUDE,
        )
        is None
    )
    # An ordinary tool error without a structured denial marker is not a denial.
    assert (
        parse_denial_event(
            json.dumps({"type": "tool_use_result", "is_error": True}),
            denial_loop.CLAUDE,
        )
        is None
    )
    # Text or non-JSON lines are ignored.
    assert parse_denial_event("permission denied", denial_loop.CLAUDE) is None
    assert parse_denial_event("{broken", denial_loop.CLAUDE) is None
    assert parse_denial_event(_claude_denial_line(), "python3") is None


def test_denial_marker_must_be_explicitly_true() -> None:
    line = json.dumps(
        {
            "type": "tool_use_result",
            "denial": {"denied": False, "tool": "Bash", "operation": "x"},
        }
    )
    assert parse_denial_event(line, denial_loop.CLAUDE) is None


# ---------------------------------------------------------------------- gating


def test_parse_version_extracts_dotted_triple_or_none() -> None:
    assert parse_version("2.1.318 (Claude Code)") == (2, 1, 318)
    assert parse_version("codex 0.166.0") == (0, 166, 0)
    assert parse_version("unknown") is None
    assert parse_version(None) is None


def test_supports_structured_denials_gates_on_version_and_flags() -> None:
    claude_stream = ["claude", "-p", "--output-format", "stream-json"]
    assert supports_structured_denials(denial_loop.CLAUDE, claude_stream, "2.1.0")[0]
    assert not supports_structured_denials(
        denial_loop.CLAUDE, ["claude", "-p"], "2.1.0"
    )[0]
    assert not supports_structured_denials(denial_loop.CLAUDE, claude_stream, "1.9.0")[
        0
    ]
    assert not supports_structured_denials(denial_loop.CLAUDE, claude_stream, None)[0]

    assert supports_structured_denials(
        denial_loop.CODEX, ["codex", "--json"], "0.40.0"
    )[0]
    assert not supports_structured_denials(
        denial_loop.CODEX, ["codex", "exec"], "0.166.0"
    )[0]
    assert not supports_structured_denials("gemini", [], "9.9.9")[0]


def test_probe_version_is_cached_per_runner(monkeypatch) -> None:
    denial_loop._version_cache.clear()
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="claude 2.1.318", stderr="", returncode=0)

    assert denial_loop.probe_version("claude", run=fake_run) == "2.1.318"
    assert denial_loop.probe_version("claude", run=fake_run) == "2.1.318"
    assert len(calls) == 1
    denial_loop._version_cache.clear()


def test_detector_for_returns_none_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(denial_loop.config, "DENIAL_LOOP_MAX_PER_ATTEMPT", 0)
    assert (
        denial_loop.detector_for("claude", ["claude", "--output-format", "stream-json"])
        is None
    )


# ---------------------------------------------------------------------- counting


def test_detector_blocks_only_at_the_threshold() -> None:
    detector = DenialLoopDetector(max_per_attempt=3)
    line = _claude_denial_line()
    assert detector.feed(line, runner="claude", attempt_id="a") is None
    assert detector.feed(line, runner="claude", attempt_id="a") is None
    blocker = detector.feed(line, runner="claude", attempt_id="a")
    assert isinstance(blocker, DenialLoopBlockedError)
    assert blocker.count == 3


def test_detector_blocks_immediately_at_threshold_one() -> None:
    detector = DenialLoopDetector(max_per_attempt=1)
    blocker = detector.feed(_claude_denial_line(), runner="claude", attempt_id="a")
    assert isinstance(blocker, DenialLoopBlockedError)


def test_unique_denials_do_not_combine() -> None:
    detector = DenialLoopDetector(max_per_attempt=2)
    assert (
        detector.feed(_claude_denial_line("a"), runner="claude", attempt_id="a") is None
    )
    assert (
        detector.feed(_claude_denial_line("b"), runner="claude", attempt_id="a") is None
    )


def test_attempt_identity_change_resets_the_counter() -> None:
    detector = DenialLoopDetector(max_per_attempt=2)
    line = _claude_denial_line()
    assert detector.feed(line, runner="claude", attempt_id="first") is None
    assert detector.feed(line, runner="claude", attempt_id="second") is None


def test_policy_change_is_a_new_identity() -> None:
    detector = DenialLoopDetector(max_per_attempt=2)
    line_a = json.dumps(
        {
            "type": "tool_use_result",
            "tool_name": "Bash",
            "denial": {
                "denied": True,
                "tool": "Bash",
                "operation": "git push",
                "policy": "disallowed_tool",
            },
        }
    )
    line_b = json.dumps(
        {
            "type": "tool_use_result",
            "tool_name": "Bash",
            "denial": {
                "denied": True,
                "tool": "Bash",
                "operation": "git push",
                "policy": "allowed_now",
            },
        }
    )
    assert detector.feed(line_a, runner="claude", attempt_id="a") is None
    assert detector.feed(line_b, runner="claude", attempt_id="a") is None


def test_detector_ignores_text_when_disabled() -> None:
    detector = DenialLoopDetector(max_per_attempt=0)
    assert detector.feed(_claude_denial_line(), runner="claude", attempt_id="a") is None


# ---------------------------------------------------------------------- blocker


def test_blocker_is_a_precise_no_code_outcome() -> None:
    event = parse_denial_event(_claude_denial_line("git push origin main"), "claude")
    assert event is not None
    blocker = DenialLoopBlockedError(event, 4)
    assert isinstance(blocker, NoCodeOutcomeError)
    assert blocker.status == "blocked"
    assert "4 times" in blocker.report
    assert event.operation in blocker.report
    assert "git push" not in blocker.report
    assert "Bash" in blocker.report


def test_denial_event_identity_is_stable_and_hashed() -> None:
    event = DenialEvent(
        runner="claude", tool="Bash", operation="abc123", policy="denied"
    )
    assert event.identity() == ("claude", "Bash", "abc123", "denied")
