"""Provider quota mapping, safe caching and bounded subprocess lifecycle."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from ai_agent import codex_limits, provider_limits


class QuotaTests(unittest.TestCase):
    def test_named_buckets_take_precedence_and_exclude_account_secrets(self):
        result = {
            "accountId": "SECRET",
            "rateLimits": {"primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "usedPercent": 40,
                        "windowDurationMins": 300,
                        "resetsAt": 1000,
                    },
                    "secondary": {"usedPercent": 100, "windowDurationMins": 10080},
                    "credits": {"balance": "PRIVATE"},
                }
            },
        }
        windows = codex_limits.quota_windows(result)
        self.assertEqual([entry["remaining_percent"] for entry in windows], [60, 0])
        self.assertEqual(windows[0]["window_minutes"], 300)
        self.assertNotIn("SECRET", json.dumps(windows))
        self.assertNotIn("PRIVATE", json.dumps(windows))

    def test_legacy_bucket_and_missing_windows(self):
        windows = codex_limits.quota_windows(
            {"rateLimits": {"primary": {"usedPercent": 120}}}
        )
        self.assertEqual(windows[0]["remaining_percent"], 0)
        self.assertEqual(codex_limits.quota_windows({"rateLimits": {}}), [])
        self.assertEqual(
            codex_limits.quota_windows(
                {"rateLimits": {"primary": {"usedPercent": None}}}
            ),
            [],
        )


class ReadTests(unittest.IsolatedAsyncioTestCase):
    def process(self):
        process = MagicMock()
        process.pid = 12345
        process.returncode = None
        process.stdin.drain = AsyncMock()
        process.wait = AsyncMock()
        return process

    async def test_handshake_ignores_notifications_and_reads_windows(self):
        process = self.process()
        process.stdout.readline = AsyncMock(
            side_effect=[
                b'{"id":1,"result":{}}\n',
                b'{"method":"notice"}\n',
                b'{"id":2,"result":{"rateLimits":{"primary":{"usedPercent":30}}}}\n',
            ]
        )
        with (
            patch.object(
                asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
            ),
            patch.object(codex_limits.os, "killpg") as kill,
        ):
            windows = await codex_limits.read_codex_limits()
        self.assertEqual(windows[0]["remaining_percent"], 70)
        messages = [
            json.loads(call.args[0]) for call in process.stdin.write.call_args_list
        ]
        self.assertEqual(
            [message["method"] for message in messages],
            ["initialize", "initialized", "account/rateLimits/read"],
        )
        kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_timeout_kills_process_group_and_waits(self):
        process = self.process()

        async def never_answers():
            await asyncio.Future()

        process.stdout.readline = never_answers
        with (
            patch.object(
                asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
            ),
            patch.object(codex_limits, "LIMIT_TIMEOUT_SECONDS", 0.01),
            patch.object(codex_limits.os, "killpg") as kill,
        ):
            with self.assertRaises(asyncio.TimeoutError):
                await codex_limits.read_codex_limits()
        kill.assert_called_once()
        process.wait.assert_awaited_once()


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cached = patch.dict(provider_limits._LIMITS, {}, clear=True)
        self.cached.start()
        self.addCleanup(self.cached.stop)

    async def test_failed_refresh_keeps_last_reading_marked_unavailable(self):
        provider_limits._LIMITS["codex"] = {
            "status": "ok",
            "checked_at": 123,
            "windows": [{"remaining_percent": 10}],
        }
        with patch.object(
            provider_limits,
            "read_codex_limits",
            AsyncMock(side_effect=RuntimeError("SECRET")),
        ):
            await provider_limits.refresh_codex_limits()
        result = provider_limits.limits_snapshot()["codex"]
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["checked_at"], 123)
        self.assertEqual(result["windows"][0]["remaining_percent"], 10)
        self.assertNotIn("SECRET", json.dumps(result))

    async def test_snapshot_does_not_mutate_cache(self):
        with patch.object(
            provider_limits,
            "read_codex_limits",
            AsyncMock(return_value=[{"remaining_percent": 50}]),
        ):
            await provider_limits.refresh_codex_limits()
        result = provider_limits.limits_snapshot()
        result["codex"]["windows"][0]["remaining_percent"] = 99
        self.assertEqual(
            provider_limits.limits_snapshot()["codex"]["windows"][0][
                "remaining_percent"
            ],
            50,
        )

    def test_claude_caches_headers_only_and_explains_missing_key(self):
        provider_limits.cache_claude_limits(
            200,
            {
                "anthropic-ratelimit-requests-limit": "100",
                "anthropic-ratelimit-requests-remaining": "75",
                "x-api-key": "SECRET",
            },
        )
        data = provider_limits.limits_snapshot()
        self.assertEqual(data["claude"]["windows"][0]["remaining"], "75")
        self.assertNotIn("SECRET", json.dumps(data))
        provider_limits._LIMITS.clear()
        with patch.object(provider_limits, "ANTHROPIC_KEY", ""):
            self.assertEqual(
                provider_limits.limits_snapshot()["claude"]["status"], "not_configured"
            )
