"""Provider availability classification and the worker's registry-driven fallback."""

import importlib
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from ai_agent.provider_availability import (
    AUTH_BLOCKED,
    AVAILABLE,
    DISABLED,
    EXECUTION,
    FUNDS_INSUFFICIENT,
    TRANSIENT,
    availability,
    classify_failure,
)

NOW = 1_800_000_000.0


class AvailabilityTests(unittest.TestCase):
    def test_unknown_and_disabled_providers_are_disabled(self) -> None:
        self.assertEqual(availability("gemini", now=NOW), DISABLED)
        self.assertEqual(availability("opencode", now=NOW), DISABLED)

    def test_enabled_deepseek_is_available_without_a_blocking_reading(self) -> None:
        self.assertEqual(availability("deepseek", now=NOW), AVAILABLE)

    def test_logged_out_and_unavailable_auth_block_the_tool(self) -> None:
        for status in ("logged_out", "unavailable"):
            with self.subTest(status=status):
                self.assertEqual(
                    availability("codex", auth={"codex": {"status": status}}, now=NOW),
                    AUTH_BLOCKED,
                )

    def test_configured_unverified_auth_is_usable(self) -> None:
        self.assertEqual(
            availability(
                "codex",
                auth={"codex": {"status": "configured_unverified"}},
                now=NOW,
            ),
            AVAILABLE,
        )
        self.assertEqual(
            availability("codex", auth={"codex": {"status": "unknown"}}), AVAILABLE
        )

    def test_funds_reading_blocks_without_inventing_a_reset(self) -> None:
        self.assertEqual(
            availability(
                "claude",
                limits={"claude_code": {"collection_error": "api_spending_limit"}},
                now=NOW,
            ),
            FUNDS_INSUFFICIENT,
        )
        self.assertEqual(
            availability(
                "claude",
                limits={"claude_code": {"spending_status": "exceeded"}},
                now=NOW,
            ),
            FUNDS_INSUFFICIENT,
        )

    def test_rate_window_below_reserve_is_transient(self) -> None:
        limits = {
            "codex": {"windows": [{"remaining_percent": 20, "resets_at": NOW + 3600}]}
        }
        self.assertEqual(
            availability("codex", limits=limits, now=NOW, reserve=30), TRANSIENT
        )
        self.assertEqual(
            availability("codex", limits=limits, now=NOW, reserve=10), AVAILABLE
        )

    def test_persisted_out_until_is_transient(self) -> None:
        self.assertEqual(
            availability("codex", out_until={"codex": NOW + 600}, now=NOW),
            TRANSIENT,
        )
        self.assertEqual(
            availability("codex", out_until={"codex": NOW - 1}, now=NOW),
            AVAILABLE,
        )

    def test_claude_runner_is_independent_of_native_anthropic_auth(self) -> None:
        # "claude" in the worker means the claude-code runner; a logged-out
        # native-Anthropic API reading must never gate the runner.
        auth = {
            "claude": {"status": "logged_out"},
            "claude-code": {"status": "authenticated"},
        }
        self.assertEqual(availability("claude", auth=auth, now=NOW), AVAILABLE)


class ClassifyFailureTests(unittest.TestCase):
    def test_transient_failures(self) -> None:
        for text in (
            "usage limit reached",
            "rate limit exceeded",
            "429 Too Many Requests",
            "connection reset",
            "network unreachable",
            "timed out",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_failure(text), TRANSIENT)
        self.assertEqual(classify_failure("throttled", status_code=429), TRANSIENT)

    def test_funds_failures_are_checked_before_transient(self) -> None:
        for text in (
            "api spending limit reached",
            "insufficient funds",
            "billing problem",
            "out of credits",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_failure(text), FUNDS_INSUFFICIENT)

    def test_anything_else_is_an_execution_failure(self) -> None:
        for text in ("", None, "the agent made no file changes"):
            with self.subTest(text=text):
                self.assertEqual(classify_failure(text), EXECUTION)


class PickToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        self.previous_anthropic = sys.modules.get("anthropic")
        if self.previous_anthropic is None:
            fake = types.ModuleType("anthropic")
            fake.Anthropic = lambda api_key, **kwargs: object()
            fake.DefaultHttpxClient = lambda **kwargs: object()
            fake.NotFoundError = type("NotFoundError", (Exception,), {})
            sys.modules["anthropic"] = fake
        self.worker = importlib.import_module("ai_agent.worker")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.previous_env)
        if self.previous_anthropic is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self.previous_anthropic

    def test_codex_fallback_when_claude_is_logged_out(self) -> None:
        tool, note = self.worker.pick_tool(
            "claude",
            {},
            {"reserve": 30, "auto_tool": True},
            NOW,
            auth={"claude-code": {"status": "logged_out"}},
        )
        self.assertEqual(tool, "codex")
        self.assertIn("claude is logged_out", note)

    def test_disabled_native_anthropic_does_not_block_the_runner(self) -> None:
        tool, note = self.worker.pick_tool(
            "claude",
            {},
            {"reserve": 30, "auto_tool": False},
            NOW,
            auth={"claude": {"status": "logged_out"}},
        )
        self.assertEqual((tool, note), ("claude", ""))

    def test_unknown_preferred_falls_back_to_a_wired_tool(self) -> None:
        tool, note = self.worker.pick_tool(
            "gemini", {}, {"reserve": 30, "auto_tool": True}, NOW
        )
        self.assertEqual(tool, "codex")
        self.assertIn("gemini is disabled", note)

    def test_persisted_out_until_drives_fallback_and_is_not_mutated(self) -> None:
        worker = {"reserve": 30, "auto_tool": True, "out_until": {"codex": NOW + 600}}
        before = dict(worker["out_until"])
        tool, note = self.worker.pick_tool("codex", {}, worker, NOW)
        self.assertEqual(tool, "claude")
        self.assertIn("codex hit its usage limit", note)
        self.assertEqual(worker["out_until"], before)


class TokenRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_code_token_route_skips_native_login_probe(self) -> None:
        from ai_agent import tool_auth_providers as providers

        with (
            patch.object(providers, "CLAUDE_CODE_AUTH", "api_key"),
            patch.object(providers, "run_probe", AsyncMock()) as run,
        ):
            reading = await providers.check("claude-code")
        self.assertEqual(
            (reading.status, reading.method), ("configured_unverified", "api_key")
        )
        run.assert_not_awaited()
