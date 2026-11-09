"""Claude Code stream-json usage: dedupe, provider/model attribution, parity.

The fixtures under ``tests/fixtures/claude_code/`` pin sanitized stream-json
shapes (success/cancel/fail/stream/plan) plus an independent ``audit`` block
with the deduplicated totals. These tests replay each fixture and assert the
parser matches the audit exactly, and that no raw content or secret ever
reaches the parsed result.
"""

import json
from pathlib import Path

import pytest

from ai_agent.claude_code_usage import (
    PRICE_LIST_SOURCE,
    ClaudeCodeUsage,
    claude_code_stream_args,
    estimate_fields,
    parse_stream,
    provider_for_model,
    result_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"

SCENARIOS = ("success", "cancel", "fail", "stream", "plan")


def _load(scenario: str) -> dict:
    return json.loads((FIXTURES / f"{scenario}.json").read_text(encoding="utf-8"))


def _stream(fixture: dict) -> str:
    return "\n".join(json.dumps(event) for event in fixture["events"]) + "\n"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_replay_matches_the_independent_audit_totals(scenario: str) -> None:
    fixture = _load(scenario)
    usage = parse_stream(_stream(fixture))
    for key, expected in fixture["audit"].items():
        assert getattr(usage, key) == expected, key


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fixtures_are_sanitized_and_secret_free(scenario: str) -> None:
    raw = (FIXTURES / f"{scenario}.json").read_text(encoding="utf-8")
    for forbidden in (
        "sk-ant",
        "SECRET",
        "api_key",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "Bearer ",
    ):
        assert forbidden not in raw
    # The stream may name where the key came from; it must never carry one.
    if "apiKeySource" in raw:
        assert '"apiKeySource": "none"' in raw


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_parsed_usage_never_holds_raw_stream_content(scenario: str) -> None:
    fixture = _load(scenario)
    usage = parse_stream(_stream(fixture))
    text = json.dumps(
        {
            name: getattr(usage, name)
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "reasoning_tokens",
                "total_tokens",
                "cost_usd",
                "model",
                "provider",
                "complete",
                "cancelled",
                "is_error",
            )
        }
    )
    for raw_word in ("edited", "Done", "Cancelled by user", "Not logged in"):
        assert raw_word not in text


def test_wrapper_total_is_never_added_to_per_request_usage() -> None:
    stream = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
            {
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "usage": {"input_tokens": 600, "output_tokens": 150},
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "usage": {"input_tokens": 600, "output_tokens": 150},
            },
        )
    )
    usage = parse_stream(stream)
    assert usage.total_tokens == 750
    assert usage.input_tokens == 600
    assert usage.output_tokens == 150


def test_stream_and_reconnect_re_emissions_are_deduplicated() -> None:
    stream = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
            {
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "message_delta",
                    "message": {"id": "msg-1", "usage": {"input_tokens": 10}},
                },
            },
            {"type": "reconnect", "reason": "resume"},
            {
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        )
    )
    usage = parse_stream(stream)
    assert usage.total_tokens == 15
    assert usage.input_tokens == 10


def test_provider_derives_only_from_the_reported_model_id() -> None:
    assert provider_for_model("deepseek-v4-pro") == "deepseek"
    assert provider_for_model("claude-sonnet-4-6") == "claude"
    assert provider_for_model("gpt-4o") is None
    assert provider_for_model(None) is None
    assert provider_for_model("") is None


def test_missing_or_empty_stream_is_unknown_never_zero() -> None:
    for text in ("", "not json\n", "plain prose\n"):
        usage = parse_stream(text)
        assert usage.total_tokens is None
        assert usage.input_tokens is None
        assert usage.output_tokens is None
        assert usage.complete is False
        assert usage.cancelled is False
        assert usage.is_error is False


def test_result_text_returns_the_final_prose_only() -> None:
    stream = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"},
            {
                "type": "result",
                "subtype": "success",
                "result": "Done",
                "is_error": False,
            },
        )
    )
    assert result_text(stream) == "Done"
    assert result_text("no result here") == ""


def test_claude_code_stream_args_are_shared() -> None:
    assert claude_code_stream_args() == [
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]


def test_estimate_is_computed_only_from_dated_list_prices() -> None:
    estimate = estimate_fields("deepseek-v4-pro", 1_000_000, 1_000_000)
    assert estimate["estimate_usd"] == 1.12
    assert estimate["estimate_currency"] == "USD"
    assert estimate["estimate_source"] == PRICE_LIST_SOURCE
    assert estimate["estimate_rate"] == "peak"
    assert estimate["estimate_missing_rate"] is False


def test_estimate_missing_rate_is_flagged_never_invented() -> None:
    estimate = estimate_fields("claude-sonnet-4-6", 10, 5)
    assert estimate == {
        "estimate_usd": None,
        "estimate_currency": None,
        "estimate_source": None,
        "estimate_rate": None,
        "estimate_missing_rate": True,
    }


def test_estimate_with_unknown_tokens_stays_none_but_keeps_the_rate() -> None:
    estimate = estimate_fields("deepseek-v4-pro", None, 5)
    assert estimate["estimate_usd"] is None
    assert estimate["estimate_currency"] == "USD"
    assert estimate["estimate_rate"] == "peak"
    assert estimate["estimate_missing_rate"] is False


def test_usage_dataclass_total_is_cache_safe() -> None:
    usage = ClaudeCodeUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=99,
        cache_creation_tokens=50,
        reasoning_tokens=7,
        total_tokens=15,
    )
    assert usage.tokens() == 15
