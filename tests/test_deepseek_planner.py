"""Native DeepSeek planner: mocked-HTTP behavior tests.

No real network: ``urllib.request.urlopen`` is mocked, and usage recording is
patched so the accounting contract (one accounted repair, auth/funds/transient
classification, reasoning tokens kept separate and never echoed) is asserted
without touching the shared stores.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from ai_agent import deepseek_planner as dp


class FakeResponse:
    """A context-manager response with a bounded, consuming ``read``."""

    def __init__(self, body: bytes, status: int = 200, headers=None) -> None:
        self._body = body
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data


def _chat_response(
    text: str,
    finish: str = "stop",
    usage: dict | None = None,
    reasoning: str | None = None,
) -> bytes:
    message = {"role": "assistant", "content": text}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    payload = {
        "id": "cmpl-1",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }
    return json.dumps(payload).encode("utf-8")


class PlannerMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = '{"branch": "feature/x", "summary": "ok", "files": [], "steps": []}'

    def test_json_success_records_usage_without_reasoning(self) -> None:
        body = _chat_response(
            self.plan,
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 19,
                "completion_tokens_details": {"reasoning_tokens": 40},
            },
            reasoning="SECRET thinking",
        )
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(
                dp.urllib.request, "urlopen", return_value=FakeResponse(body)
            ) as urlopen,
            patch.object(dp, "record_usage") as record_usage,
            patch.object(dp, "record_detail") as record_detail,
        ):
            text = dp.planner_message("plan me", "feature_plan.json", 4000)

        self.assertEqual(text, self.plan)
        self.assertEqual(urlopen.call_count, 1)
        record_usage.assert_called_once()
        record = record_usage.call_args.args[0]
        self.assertEqual(
            (record.role, record.tool, record.tokens),
            ("planner", "deepseek", 19),
        )
        detail = record_detail.call_args.args[0]
        self.assertEqual(detail.runner, "deepseek-api")
        self.assertEqual(detail.provider, "deepseek")
        self.assertEqual(detail.model, dp.DEEPSEEK_MODEL)
        self.assertEqual(detail.input_tokens, 12)
        self.assertEqual(detail.output_tokens, 7)
        self.assertEqual(detail.total_tokens, 19)
        self.assertEqual(detail.reasoning_tokens, 40)

    def test_truncation_gets_exactly_one_accounted_repair(self) -> None:
        truncated = _chat_response('{"branch": "fe', finish="length")
        repaired = _chat_response(self.plan, finish="stop")
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(
                dp.urllib.request,
                "urlopen",
                side_effect=[FakeResponse(truncated), FakeResponse(repaired)],
            ) as urlopen,
            patch.object(dp, "record_usage"),
            patch.object(dp, "record_detail"),
        ):
            text = dp.planner_message("plan me", "feature_plan.json", 4000)

        self.assertEqual(text, self.plan)
        self.assertEqual(urlopen.call_count, 2)

    def test_second_truncation_fails_instead_of_looping(self) -> None:
        truncated = _chat_response('{"branch": "fe', finish="length")
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(
                dp.urllib.request,
                "urlopen",
                side_effect=[FakeResponse(truncated), FakeResponse(truncated)],
            ) as urlopen,
            patch.object(dp, "record_usage"),
            patch.object(dp, "record_detail"),
        ):
            with self.assertRaises(dp.DeepSeekTruncatedError):
                dp.planner_message("plan me", "feature_plan.json", 4000)
        self.assertEqual(urlopen.call_count, 2)

    def test_missing_api_key_is_an_auth_error(self) -> None:
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", ""),
            patch("ai_agent.image_delivery.attachments", return_value=([], 0)),
        ):
            with self.assertRaises(dp.DeepSeekAuthError):
                dp.planner_message("plan me", "feature_plan.json", 4000)

    def test_images_are_rejected_never_dropped(self) -> None:
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch(
                "ai_agent.image_delivery.attachments",
                return_value=([("image/png", b"\x89PNG")], 0),
            ),
            patch.object(dp.urllib.request, "urlopen") as urlopen,
        ):
            with self.assertRaisesRegex(dp.DeepSeekError, "image"):
                dp.planner_message("plan me", "feature_plan.json", 4000)
        urlopen.assert_not_called()

    def test_network_failure_is_transient(self) -> None:
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(
                dp.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("timed out"),
            ),
        ):
            with self.assertRaises(dp.DeepSeekTransientError):
                dp.planner_message("plan me", "feature_plan.json", 4000)

    def test_oversized_reply_exceeds_the_output_bound(self) -> None:
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(dp, "DEEPSEEK_MAX_OUTPUT_CHARS", 8),
            patch.object(
                dp.urllib.request,
                "urlopen",
                return_value=FakeResponse(b"x" * 40),
            ),
        ):
            with self.assertRaisesRegex(dp.DeepSeekError, "output bound"):
                dp.planner_message("plan me", "feature_plan.json", 4000)


class HttpErrorClassificationTests(unittest.TestCase):
    def test_auth_funds_and_transient_statuses(self) -> None:
        self.assertIsInstance(dp.classify_http_error(401, "nope"), dp.DeepSeekAuthError)
        self.assertIsInstance(
            dp.classify_http_error(402, "nope"), dp.DeepSeekFundsError
        )
        self.assertIsInstance(
            dp.classify_http_error(429, "nope"), dp.DeepSeekTransientError
        )
        self.assertIsInstance(
            dp.classify_http_error(503, "nope"), dp.DeepSeekTransientError
        )

    def test_chat_completion_raises_classified_http_errors(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/v1/chat/completions",
            402,
            "Payment Required",
            {"content-type": "application/json"},
            io.BytesIO(b"insufficient funds"),
        )
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(dp.urllib.request, "urlopen", side_effect=error),
        ):
            with self.assertRaises(dp.DeepSeekFundsError):
                dp._chat_completion([{"role": "user", "content": "hi"}], 400, "m")

    def test_error_bodies_are_truncated_never_echoed_in_full(self) -> None:
        body = "secret-key " * 2000
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/v1/chat/completions",
            500,
            "Server Error",
            {},
            io.BytesIO(body.encode("utf-8")),
        )
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(dp.urllib.request, "urlopen", side_effect=error),
        ):
            with self.assertRaises(dp.DeepSeekError) as raised:
                dp._chat_completion([{"role": "user", "content": "hi"}], 400, "m")
        self.assertLess(len(str(raised.exception)), 400)


class DiscoveryTests(unittest.TestCase):
    def test_discover_models_prepends_configured_models(self) -> None:
        body = json.dumps(
            {"data": [{"id": "deepseek-chat", "display_name": "DeepSeek Chat"}]}
        ).encode("utf-8")
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(
                dp.urllib.request, "urlopen", return_value=FakeResponse(body)
            ) as urlopen,
        ):
            models = dp.discover_models()

        ids = [entry["id"] for entry in models]
        self.assertEqual(ids[0], dp.DEEPSEEK_MODEL)
        self.assertEqual(ids[1], dp.DEEPSEEK_FLASH_MODEL)
        self.assertIn("deepseek-chat", ids)
        urlopen.assert_called_once()

    def test_discover_balance_returns_whitelisted_fields_only(self) -> None:
        body = json.dumps(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "10.00",
                        "granted_balance": "5.00",
                        "topped_up_balance": "5.00",
                        "secret_field": "SECRET",
                    }
                ],
            }
        ).encode("utf-8")
        with (
            patch.object(dp, "DEEPSEEK_API_KEY", "sk-test"),
            patch.object(
                dp.urllib.request, "urlopen", return_value=FakeResponse(body)
            ),
        ):
            balance = dp.discover_balance()

        self.assertTrue(balance["is_available"])
        self.assertEqual(balance["currency"], "CNY")
        self.assertNotIn("secret_field", balance)


class UsageFieldTests(unittest.TestCase):
    def test_total_excludes_reasoning_tokens(self) -> None:
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 99},
        }
        self.assertEqual(dp.deepseek_total_tokens(usage), 15)
        self.assertEqual(dp.deepseek_usage_fields(usage)["reasoning_tokens"], 99)

    def test_unusable_counts_are_none_not_zero(self) -> None:
        self.assertIsNone(dp.deepseek_total_tokens(None))
        self.assertIsNone(dp.deepseek_total_tokens({"prompt_tokens": "x"}))
        self.assertIsNone(dp.deepseek_total_tokens({"prompt_tokens": -1}))


if __name__ == "__main__":
    unittest.main()
