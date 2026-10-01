"""Passive Claude Code quota capture, expiry and status-line installation."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_agent import provider_limits
from ai_agent.claude_code_limits import capture_limits, read_limits
from ai_agent.claude_statusline_setup import install_statusline


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "claude-code-limits.json"
        self.payload = {
            "session_id": "SECRET",
            "rate_limits": {
                "five_hour": {"used_percentage": 100, "resets_at": 2000},
                "seven_day": {"used_percentage": 35, "resets_at": 4000},
            },
        }

    def test_captures_secret_free_owner_only_windows(self):
        self.assertTrue(capture_limits(self.payload, self.path, now=1000))
        text = self.path.read_text()
        self.assertNotIn("SECRET", text)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        reading = read_limits(self.path, now=1000)
        self.assertEqual(
            [entry["remaining_percent"] for entry in reading["windows"]], [0, 65]
        )
        self.assertEqual(reading["checked_at"], 1000)

    def test_missing_rate_limits_keeps_last_reading(self):
        capture_limits(self.payload, self.path, now=1000)
        previous = self.path.read_text()
        self.assertFalse(capture_limits({"session_id": "API session"}, self.path))
        self.assertEqual(self.path.read_text(), previous)

    def test_expired_window_is_removed_without_assuming_reset(self):
        capture_limits(self.payload, self.path, now=1000)
        partial = read_limits(self.path, now=2500)
        self.assertEqual(len(partial["windows"]), 1)
        self.assertEqual(partial["windows"][0]["window_minutes"], 10080)
        expired = read_limits(self.path, now=4000)
        self.assertEqual(expired["windows"], [])
        self.assertEqual(expired["status"], "unavailable")

    def test_invalid_values_do_not_become_zero_usage(self):
        self.payload["rate_limits"]["five_hour"]["used_percentage"] = None
        capture_limits(self.payload, self.path, now=1000)
        self.assertEqual(len(read_limits(self.path, now=1000)["windows"]), 1)

    def test_missing_and_corrupt_files_have_explanatory_states(self):
        self.assertEqual(read_limits(self.path)["status"], "not_checked")
        self.path.write_text("broken")
        self.assertEqual(read_limits(self.path)["status"], "unavailable")

    def test_snapshot_has_separate_subscription_and_api_entries(self):
        with patch.object(provider_limits, "CLAUDE_CODE_LIMITS_FILE", self.path):
            reading = provider_limits.limits_snapshot()
        self.assertIn("claude", reading)
        self.assertEqual(reading["claude_code"]["status"], "not_checked")

    def test_statusline_executable_preserves_forwarded_output(self):
        script = (
            Path(__file__).resolve().parents[1] / "deploy/claude-code-statusline.py"
        )
        self.payload["rate_limits"]["five_hour"]["resets_at"] = 9999999999
        environment = {**os.environ, "CLAUDE_CODE_LIMITS_FILE": str(self.path)}
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--forward-command",
                "printf 'my existing status'",
            ],
            input=json.dumps(self.payload),
            text=True,
            capture_output=True,
            env=environment,
            timeout=10,
            check=True,
        )
        self.assertEqual(result.stdout, "my existing status")
        self.assertNotIn("SECRET", self.path.read_text())


class InstallTests(unittest.TestCase):
    def test_install_is_idempotent_and_preserves_existing_settings(self):
        settings = Path(tempfile.mkdtemp()) / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "permissions": {"allow": []},
                    "statusLine": {
                        "type": "command",
                        "command": "echo original",
                        "padding": 2,
                    },
                }
            )
        )
        script = Path("/opt/ai-coding-agent/deploy/claude-code-statusline.py")
        self.assertTrue(install_statusline(settings, script))
        backup = json.loads(
            settings.with_suffix(".json.before-quota-capture").read_text()
        )
        self.assertEqual(backup["statusLine"]["command"], "echo original")
        configured = json.loads(settings.read_text())
        self.assertEqual(configured["permissions"], {"allow": []})
        self.assertEqual(configured["statusLine"]["padding"], 2)
        self.assertIn("--forward-command", configured["statusLine"]["command"])
        self.assertIn("echo original", configured["statusLine"]["command"])
        self.assertFalse(install_statusline(settings, script))
