import asyncio
import json
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
        before = self.path.read_text()
        self.assertFalse(claude_code_probe.probe(self.path, lambda argv: "", NOW + 1))
        self.assertEqual(self.path.read_text(), before)

    def test_zero_minutes_switches_it_off(self) -> None:
        with patch.object(claude_code_probe, "probe") as probe:
            asyncio.run(claude_code_probe.probe_forever(self.path, minutes=0))
        probe.assert_not_called()
