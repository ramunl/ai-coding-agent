"""Limits use recorded usage and never spend tokens for a read."""

from types import SimpleNamespace
from unittest.mock import patch

from ai_agent import anthropic_limits, provider_limits


def test_read_limits_does_not_probe_and_survives_cache_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_limits, "CLAUDE_CACHE_FILE", tmp_path / "limits.json")
    with patch.dict(provider_limits._LIMITS, {}, clear=True):
        provider_limits.record_claude_response(
            SimpleNamespace(
                status_code=200,
                headers={
                    "anthropic-ratelimit-requests-limit": "100",
                    "anthropic-ratelimit-requests-remaining": "80",
                    "x-api-key": "SECRET",
                },
            )
        )
        provider_limits._LIMITS.clear()
        with patch.object(
            anthropic_limits,
            "anthropic_limit_headers",
            side_effect=AssertionError("No probes allowed"),
        ):
            text = anthropic_limits.get_anthropic_limits()
        assert "80/100" in text
        assert "Read " in text
        assert "SECRET" not in provider_limits.CLAUDE_CACHE_FILE.read_text()


def test_response_without_headers_preserves_previous_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_limits, "CLAUDE_CACHE_FILE", tmp_path / "limits.json")
    with patch.dict(provider_limits._LIMITS, {}, clear=True):
        provider_limits.cache_claude_limits(
            200, {"anthropic-ratelimit-requests-limit": "100"}
        )
        original = provider_limits.limits_snapshot()["claude"]
        provider_limits.cache_claude_limits(500, {})
        assert provider_limits.limits_snapshot()["claude"] == original


def test_real_sdk_response_hook_records_usage_without_an_extra_request(
    tmp_path, monkeypatch
):
    """The SDK's real response path updates the cache from one planning call."""
    import anthropic
    from anthropic import _base_client

    httpx = getattr(_base_client, "httpx2", None) or getattr(_base_client, "httpx")

    monkeypatch.setattr(provider_limits, "CLAUDE_CACHE_FILE", tmp_path / "limits.json")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={
                "anthropic-ratelimit-requests-limit": "100",
                "anthropic-ratelimit-requests-remaining": "99",
            },
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "test",
                "content": [{"type": "text", "text": "plan"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    with patch.dict(provider_limits._LIMITS, {}, clear=True):
        with anthropic.Anthropic(
            api_key="test",
            http_client=anthropic.DefaultHttpxClient(
                transport=httpx.MockTransport(respond),
                event_hooks={"response": [provider_limits.record_claude_response]},
            ),
        ) as client:
            answer = client.messages.create(
                model="test",
                max_tokens=1,
                messages=[{"role": "user", "content": "plan"}],
            )
        assert answer.content[0].text == "plan"
        assert len(calls) == 1
        assert (
            provider_limits.limits_snapshot()["claude"]["windows"][0]["remaining"]
            == "99"
        )


def test_last_model_switch_survives_unrelated_result_rotation(tmp_path):
    from ai_agent.inbox import ResultLog

    log = ResultLog(tmp_path / "results.json")
    log.record("model", "switch_model", "failed", "Restart failed")
    for index in range(25):
        log.record(str(index), "set_planner", "done", "Selected")
    reloaded = ResultLog(log.path)
    assert len(reloaded.results) == 20
    assert any(item["id"] == "model" for item in reloaded.results)
