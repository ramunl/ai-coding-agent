import unittest

from ai_agent.anthropic_limits import format_anthropic_limits, format_limit_row


class AnthropicLimitsTests(unittest.TestCase):
    def test_format_limit_row_with_remaining_limit_and_reset(self) -> None:
        headers = {
            "anthropic-ratelimit-requests-limit": "100",
            "anthropic-ratelimit-requests-remaining": "42",
            "anthropic-ratelimit-requests-reset": "2026-05-21T12:00:00Z",
        }

        row = format_limit_row(headers, "requests", "Requests")

        self.assertEqual(row, "- Requests, 42/100 remaining, resets 2026-05-21T12:00:00Z")

    def test_format_limit_row_returns_none_when_headers_are_missing(self) -> None:
        self.assertIsNone(format_limit_row({}, "requests", "Requests"))

    def test_format_anthropic_limits_includes_error_body_for_http_error(self) -> None:
        message = format_anthropic_limits(400, {}, '{"error":"low balance"}')

        self.assertIn("No Anthropic rate-limit headers were returned.", message)
        self.assertIn("Anthropic returned HTTP 400", message)
        self.assertIn("low balance", message)


if __name__ == "__main__":
    unittest.main()
