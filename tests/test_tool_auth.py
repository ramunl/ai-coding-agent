"""Authentication observations must never expose provider secrets."""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

from ai_agent import tool_auth
from ai_agent import tool_auth_providers as providers
from ai_agent.tool_auth import ToolAuth, auth_record
from ai_agent.tool_auth_providers import Reading


class ParserTests(unittest.TestCase):
    def test_codex_statuses(self):
        for code, output, status in (
            (0, "Logged in using ChatGPT", "authenticated"),
            (0, "Logged in using an API key - SECRET", "configured_unverified"),
            (1, "Not logged in", "logged_out"),
            (1, "quota exceeded SECRET", "unknown"),
            (1, "missing /secret/auth.json", "unknown"),
            (0, "new format", "unknown"),
        ):
            reading = providers.parse_codex(code, output)
            self.assertEqual(reading.status, status)
            self.assertNotIn("SECRET", repr(reading))

    def test_claude_statuses_and_ignored_expiry(self):
        for method, status in (
            ("claude.ai", "authenticated"),
            ("oauth_token", "authenticated"),
            ("api_key", "configured_unverified"),
            ("api_key_helper", "configured_unverified"),
            ("third_party", "configured_unverified"),
            ("SECRET", "unknown"),
        ):
            reading = providers.parse_claude(
                0,
                json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": method,
                        "email": "SECRET",
                        "accessToken": "SECRET",
                        "expiresAt": 1,
                        "relogin_at": 1,
                    }
                ),
            )
            self.assertEqual(reading.status, status)
            self.assertIsNone(reading.relogin_at)
            self.assertNotIn("SECRET", repr(reading))
        for text in ("invalid SECRET", "[]", "null", "{}", '{"loggedIn":"true"}'):
            self.assertEqual(providers.parse_claude(0, text).status, "unknown")
        self.assertEqual(
            providers.parse_claude(1, '{"loggedIn":false,"authMethod":"none"}').status,
            "logged_out",
        )

    def test_deadline_boundaries(self):
        with patch.object(tool_auth.time, "time", return_value=100):
            for seconds, days, warning in (
                (604801, 8, None),
                (604800, 7, "relogin_soon"),
                (1, 1, "relogin_soon"),
                (0, 0, "login_required"),
                (-1, 0, "login_required"),
            ):
                record = auth_record(
                    Reading("authenticated", "oauth", 100 + seconds), 90, False
                )
                self.assertEqual(record["days_remaining"], days)
                self.assertEqual(record["warnings"], [warning] if warning else [])
            for deadline in (None, float("nan"), float("inf"), -1):
                record = auth_record(
                    Reading("authenticated", relogin_at=deadline), 90, False
                )
                self.assertIsNone(record["days_remaining"])


class PollTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_command_never_runs_status(self):
        with patch.object(
            providers, "run_probe", AsyncMock(return_value=(0, "no commands"))
        ) as run:
            self.assertEqual((await providers.check("codex")).status, "unavailable")
            self.assertEqual(run.await_count, 1)
        with patch.object(
            providers,
            "run_probe",
            AsyncMock(side_effect=[(0, "  status Show status"), (0, "--text only")]),
        ) as run:
            self.assertEqual(
                (await providers.check("claude-code")).status, "unavailable"
            )
            self.assertEqual(run.await_count, 2)

    async def test_supported_probes(self):
        with patch.object(
            providers,
            "run_probe",
            AsyncMock(
                side_effect=[
                    (0, "  status Show status"),
                    (0, "Logged in using ChatGPT"),
                ]
            ),
        ) as run:
            self.assertEqual((await providers.check("codex")).status, "authenticated")
            run.assert_awaited_with("codex", "login", "status")
        with patch.object(
            providers,
            "run_probe",
            AsyncMock(
                side_effect=[
                    (0, "  status [options] Show status"),
                    (0, "--json"),
                    (0, '{"loggedIn":true,"authMethod":"claude.ai"}'),
                ]
            ),
        ) as run:
            self.assertEqual(
                (await providers.check("claude-code")).status, "authenticated"
            )
            run.assert_awaited_with("claude", "auth", "status", "--json")

    async def test_api_key_is_only_configuration(self):
        for key, status in (("SECRET", "configured_unverified"), ("", "logged_out")):
            with patch.object(providers, "ANTHROPIC_KEY", key):
                self.assertEqual((await providers.check("claude")).status, status)

    async def test_failure_keeps_timestamp_and_excludes_errors_from_logs(self):
        cache = ToolAuth()
        self.assertTrue(cache.snapshot()["codex"]["stale"])
        with (
            patch.object(tool_auth.time, "time", return_value=100),
            patch.object(
                tool_auth,
                "check",
                AsyncMock(return_value=Reading("authenticated", "oauth")),
            ),
        ):
            await cache.refresh()
            self.assertFalse(cache.snapshot()["codex"]["stale"])
        with patch.object(tool_auth.time, "time", return_value=1000):
            self.assertTrue(cache.snapshot()["codex"]["stale"])
        with (
            patch.object(tool_auth, "check", AsyncMock(side_effect=OSError("SECRET"))),
            self.assertLogs(tool_auth.logger) as logs,
        ):
            await cache.refresh()
        record = cache.snapshot()["codex"]
        self.assertEqual(record["checked_at"], 100)
        self.assertEqual(record["status"], "authenticated")
        self.assertIn("check_unavailable", record["warnings"])
        self.assertNotIn("SECRET", str(logs.output) + json.dumps(cache.snapshot()))

    async def test_bounded_output_and_timeout(self):
        with self.assertRaises(ValueError):
            await providers.run_probe(sys.executable, "-c", "print('x' * 1000000)")
        with (
            patch.object(providers, "PROBE_TIMEOUT", 0.05),
            self.assertRaises(TimeoutError),
        ):
            await providers.run_probe(
                sys.executable, "-c", "import time; time.sleep(10)"
            )

    async def test_cancellation_reaps_subprocess(self):
        real_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            spawned.append(process)
            return process

        with patch.object(providers.asyncio, "create_subprocess_exec", spawn):
            task = asyncio.create_task(
                providers.run_probe(sys.executable, "-c", "import time; time.sleep(10)")
            )
            while not spawned:
                await asyncio.sleep(0.001)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(spawned[0].returncode)
