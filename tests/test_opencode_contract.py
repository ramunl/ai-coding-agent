"""OpenCode JSON-event contract: disabled provider and sanitized fixtures.

The fixture under ``tests/fixtures/opencode/`` records the sanitized,
versioned shape captured by ``scripts/opencode_characterize.sh``. These tests
replay it (and inline synthetic events for the other scenarios) against the
pure classification in ``ai_agent.providers.opencode``, and pin the provider's
disabled/unsupported status so a capability is never enabled by accident.
"""

import json
from pathlib import Path

from ai_agent import provider_registry
from ai_agent.provider_availability import DISABLED, availability
from ai_agent.providers import opencode

FIXTURE = Path(__file__).parent / "fixtures" / "opencode" / "success.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ fixture shape


def test_success_fixture_metadata_matches_reviewed_contract() -> None:
    fixture = _load_fixture()
    assert fixture["format_version"] == 1
    assert fixture["scenario"] == "success"
    assert fixture["tool"] == "opencode"
    assert fixture["exit_code"] == 0
    assert fixture["sanitized"] is True
    # The fixture must carry the same reviewed pin/Node values as the module.
    assert fixture["pinned_version"] == opencode.PINNED_VERSION
    assert fixture["node_requirement"] == opencode.NODE_REQUIREMENT
    assert opencode.NODE_REQUIREMENT  # never assume Node 20


def test_success_fixture_is_idempotently_sanitized() -> None:
    fixture = _load_fixture()
    # Re-sanitizing changes nothing: no secret key or value survived capture.
    assert opencode.sanitize_event(fixture) == fixture
    raw = FIXTURE.read_text(encoding="utf-8")
    assert opencode.sanitize_text(raw) == raw


def test_success_fixture_events_parse_into_the_documented_stream() -> None:
    fixture = _load_fixture()
    events = opencode.parse_events(json.dumps(fixture))
    assert events == fixture["events"]
    types = [event["type"] for event in events]
    assert types == ["message", "usage", "result"]


# ------------------------------------------------------------- replay: success


def test_success_fixture_classifies_as_clean_exit_with_usage() -> None:
    fixture = _load_fixture()
    outcome = opencode.classify_run(fixture["events"], fixture["exit_code"])
    assert outcome["exit_code"] == 0
    assert outcome["cancelled"] is False
    assert outcome["error"] is None
    assert outcome["error_kind"] is None
    assert outcome["usage"] == {
        "input_tokens": 42,
        "output_tokens": 18,
        "reasoning_tokens": 0,
        "total_tokens": 60,
    }


# --------------------------------------------------------------- error/usage/cancel


def test_error_event_classifies_into_auth_funds_and_transient() -> None:
    assert opencode.error_kind("auth_error", "bad API key") == "auth"
    assert opencode.error_kind("402", "insufficient funds") == "funds"
    assert opencode.error_kind("rate_limit", "429 too many requests") == "transient"
    assert opencode.error_kind(None, "unknown failure") == "execution"


def test_error_event_is_reported_once_and_first() -> None:
    events = [
        {"type": "error", "properties": {"code": "auth", "message": "key rejected"}},
        {"type": "error", "properties": {"code": "funds", "message": "no balance"}},
    ]
    outcome = opencode.classify_run(events, 1)
    assert outcome["error"] == "key rejected"
    assert outcome["error_kind"] == "auth"


def test_nonzero_exit_without_error_event_is_an_execution_error() -> None:
    outcome = opencode.classify_run([], 2)
    assert outcome["exit_code"] == 2
    assert outcome["cancelled"] is False
    assert outcome["error"] == "opencode exited with status 2"
    assert outcome["error_kind"] == "execution"


def test_cancelled_event_suppresses_the_synthetic_exit_error() -> None:
    events = [{"type": "cancelled", "properties": {"reason": "user"}}]
    outcome = opencode.classify_run(events, 130)
    assert outcome["cancelled"] is True
    assert outcome["error"] is None
    assert outcome["error_kind"] is None


def test_unusable_usage_counts_are_none_not_invented() -> None:
    assert opencode.usage_fields({"input_tokens": 1, "output_tokens": 2}) == {
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "total_tokens": 3,
    }
    assert opencode.usage_fields({"input_tokens": -1, "output_tokens": 2}) is None
    assert opencode.usage_fields({"input_tokens": "x", "output_tokens": 2}) is None


# ------------------------------------------------------- disabled/unsupported gate


def test_opencode_stays_disabled_and_unsupported() -> None:
    provider = provider_registry.get_provider("opencode")
    assert provider is not None
    assert provider.enabled is False
    for capability in (
        "roles",
        "adapters",
        "model",
        "auth",
        "limits",
        "errors",
        "images",
        "structured_output",
        "usage",
    ):
        assert (
            provider_registry.capability(provider, capability)
            == provider_registry.UNSUPPORTED
        ), capability
    assert provider_registry.capability(provider, "enabled") is False


def test_opencode_is_never_routed_or_available() -> None:
    assert "opencode" not in {
        p.name for p in provider_registry.enabled_providers()
    }
    assert availability("opencode") == DISABLED
