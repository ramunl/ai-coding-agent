import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_agent import claude_code_limits, claude_code_probe

NOW = 1_800_000_000.0
EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "rateLimitType": "five_hour",
        "utilization": 0.4,
        "resetsAt": int(NOW) + 3600,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.42, "resetsAt": int(NOW) + 3600},
            "seven_day": {"utilization": 0.1, "resetsAt": int(NOW) + 86400},
        },
    },
}


def stream(*events: dict) -> str:
    lines = [{"type": "system", "subtype": "init"}, *events, {"type": "result"}]
    return "\n".join(json.dumps(line) for line in lines) + "\nnot json\n"


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "claude-code-limits.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_both_windows_are_read_from_the_rate_limit_event(self) -> None:
        limits = claude_code_probe.rate_limits(stream(EVENT))
        self.assertAlmostEqual(limits["five_hour"]["used_percentage"], 42.0)
        self.assertAlmostEqual(limits["seven_day"]["used_percentage"], 10.0)
        self.assertEqual(limits["seven_day"]["resets_at"], int(NOW) + 86400)

    def test_without_per_window_data_the_top_level_window_is_used(self) -> None:
        event = json.loads(json.dumps(EVENT))
        del event["rate_limit_info"]["unifiedWindows"]
        limits = claude_code_probe.rate_limits(stream(event))
        self.assertEqual(list(limits), ["five_hour"])
        self.assertAlmostEqual(limits["five_hour"]["used_percentage"], 40.0)

    def test_a_probe_saves_windows_the_dashboard_reads(self) -> None:
        asked = []

        def runner(argv):
            asked.append(argv)
            return stream(EVENT)

        self.assertTrue(claude_code_probe.probe(self.path, runner, NOW))
        self.assertIn("stream-json", asked[0])
        self.assertIn("haiku", asked[0])
        reading = claude_code_limits.read_limits(self.path, NOW + 60)
        remaining = sorted(w["remaining_percent"] for w in reading["windows"])
        self.assertEqual([round(value) for value in remaining], [58, 90])

    def test_no_event_keeps_the_last_reading(self) -> None:
        claude_code_probe.probe(self.path, lambda argv: stream(EVENT), NOW)
        before = json.loads(self.path.read_text())
        self.assertFalse(claude_code_probe.probe(self.path, lambda argv: "", NOW + 1))
        after = json.loads(self.path.read_text())
        self.assertEqual(after["windows"], before["windows"])
        self.assertEqual(after["checked_at"], NOW)
        self.assertEqual(after["attempted_at"], NOW + 1)
        self.assertEqual(after["collection_error"], "no_valid_windows")

    def test_empty_malformed_and_expired_collection_preserves_success(self):
        claude_code_probe.probe(self.path, lambda argv: stream(EVENT), NOW)
        for data in (
            None,
            [],
            {},
            {"five_hour": {}},
            {
                "five_hour": {
                    "used_percentage": 0,
                    "resets_at": NOW - 1,
                }
            },
        ):
            self.assertFalse(
                claude_code_limits.capture_limits(
                    {"rate_limits": data}, self.path, NOW + 1
                )
            )
            reading = claude_code_limits.read_limits(self.path, NOW + 2)
            self.assertEqual(reading["checked_at"], NOW)
            self.assertEqual(len(reading["windows"]), 2)
            self.assertEqual(reading["collection_error"], "no_valid_windows")

    def test_cli_failures_are_sanitized_and_do_not_refresh_readings(self):
        claude_code_probe.probe(self.path, lambda argv: stream(EVENT), NOW)
        fixtures = [
            (
                1,
                "API Error: 400 You have reached your specified API usage limits.",
                "ANTHROPIC_API_KEY takes precedence over your claude.ai login SECRET",
                "api_spending_limit",
                True,
            ),
            (
                0,
                stream({"type": "result", "is_error": True}),
                "SECRET",
                "cli_failed",
                False,
            ),
            (1, "", "auth conflict SECRET", "authentication_conflict", True),
            (1, "", "Not logged in SECRET", "authentication_failed", False),
        ]
        for code, stdout, stderr, error, conflict in fixtures:
            result = subprocess.CompletedProcess([], code, stdout, stderr)
            self.assertFalse(
                claude_code_probe.probe(self.path, lambda argv: result, NOW + 1)
            )
            reading = claude_code_limits.read_limits(self.path, NOW + 2)
            self.assertEqual(reading["checked_at"], NOW)
            self.assertEqual(reading["collection_error"], error)
            self.assertEqual(reading["auth_conflict"], conflict)
            self.assertEqual(reading["status"], "unavailable")
            self.assertNotIn("SECRET", self.path.read_text())
        self.assertTrue(
            claude_code_probe.probe(self.path, lambda argv: stream(EVENT), NOW + 3)
        )
        self.assertIsNone(
            claude_code_limits.read_limits(self.path, NOW + 3)["collection_error"]
        )

    def test_timeout_and_missing_cli_record_attempt_without_success(self):
        for error, code in (
            (FileNotFoundError("SECRET"), "cli_unavailable"),
            (subprocess.TimeoutExpired("SECRET", 1), "timeout"),
        ):
            with patch.object(claude_code_probe, "run", side_effect=error) as runner:
                self.assertFalse(claude_code_probe.probe(self.path, runner, NOW))
            reading = claude_code_limits.read_limits(self.path, NOW)
            self.assertNotIn("checked_at", reading)
            self.assertEqual(reading["collection_error"], code)
            self.assertNotIn("SECRET", self.path.read_text())

    def test_stale_and_expired_readings_never_imply_restored_capacity(self):
        claude_code_probe.probe(self.path, lambda argv: stream(EVENT), NOW)
        stale = claude_code_limits.read_limits(self.path, NOW + 3601)
        self.assertEqual(stale["freshness"], "stale")
        self.assertEqual(stale["status"], "unavailable")
        self.assertEqual(len(stale["windows"]), 1)
        expired = claude_code_limits.read_limits(self.path, NOW + 86400)
        self.assertEqual(expired["windows"], [])
        self.assertIsNone(expired["collection_error"])

    def test_zero_minutes_switches_it_off(self) -> None:
        with patch.object(claude_code_probe, "probe") as probe:
            asyncio.run(claude_code_probe.probe_forever(self.path, minutes=0))
        probe.assert_not_called()
