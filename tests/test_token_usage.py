import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ai_agent import token_usage
from ai_agent.token_usage import (
    RETENTION_DAYS,
    UsageRecord,
    claude_total_tokens,
    codex_total_tokens,
    record_usage,
    usage_section,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TODAY = "2026-10-21"
TODAY_DATE = date(2026, 10, 21)


def _day(offset: int) -> str:
    return (TODAY_DATE - timedelta(days=offset)).isoformat()


class ParsingTests(unittest.TestCase):
    def test_codex_total_is_the_count_after_tokens_used(self) -> None:
        self.assertEqual(codex_total_tokens("done\ntokens used\n1,234\n"), 1234)
        self.assertEqual(codex_total_tokens("tokens used: 56"), 56)

    def test_codex_last_reported_total_wins(self) -> None:
        self.assertEqual(codex_total_tokens("tokens used: 5\ntokens used: 7."), 7)

    def test_codex_without_a_count_is_unknown_not_zero(self) -> None:
        self.assertIsNone(codex_total_tokens("no usage line here"))
        self.assertIsNone(codex_total_tokens(""))

    def test_claude_total_is_input_plus_output_without_cache_tokens(self) -> None:
        usage = types.SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=99
        )
        self.assertEqual(claude_total_tokens(usage), 15)

    def test_claude_missing_or_partial_usage_is_unknown(self) -> None:
        self.assertIsNone(claude_total_tokens(None))
        self.assertIsNone(claude_total_tokens(types.SimpleNamespace()))
        self.assertIsNone(claude_total_tokens(types.SimpleNamespace(input_tokens=1)))
        partial = types.SimpleNamespace(input_tokens=1, output_tokens=None)
        self.assertIsNone(claude_total_tokens(partial))
        boolean = types.SimpleNamespace(input_tokens=True, output_tokens=1)
        self.assertIsNone(claude_total_tokens(boolean))


class RecordValidationTests(unittest.TestCase):
    def test_rejects_values_outside_the_published_vocabulary(self) -> None:
        for kwargs in (
            {"role": "ops", "tool": "codex", "tokens": 1},
            {"role": "planner", "tool": "gemini", "tokens": 1},
            {"role": "planner", "tool": "codex", "tokens": True},
            {"role": "planner", "tool": "codex", "tokens": -1},
            {"role": "planner", "tool": "codex", "tokens": "Fix the login bug"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    UsageRecord(day=TODAY, **kwargs)

    def test_rejects_malformed_days(self) -> None:
        with self.assertRaises(ValueError):
            UsageRecord("planner", "codex", 1, day="2026-13-01")


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / "token-usage.json"

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def _write(self, days: dict) -> None:
        self.path.write_text(json.dumps({"format": 1, "days": days}))


class BucketAggregationTests(StoreTestCase):
    def test_calls_tokens_and_unknowns_aggregate_per_day_role_and_tool(self) -> None:
        for tokens in (100, 50, None):
            record_usage(UsageRecord("planner", "codex", tokens, TODAY), self.path)
        record_usage(UsageRecord("implementer", "claude", None, TODAY), self.path)

        days = self._read()["days"]
        self.assertEqual(
            days[TODAY]["planner"]["codex"],
            {"calls": 3, "unknown": 1, "tokens": 150},
        )
        self.assertEqual(
            days[TODAY]["implementer"]["claude"],
            {"calls": 1, "unknown": 1, "tokens": 0},
        )

    def test_missing_count_is_unknown_and_never_adds_zero_tokens(self) -> None:
        record_usage(UsageRecord("planner", "codex", 40, TODAY), self.path)
        record_usage(UsageRecord("planner", "codex", None, TODAY), self.path)
        bucket = self._read()["days"][TODAY]["planner"]["codex"]
        self.assertEqual(bucket["tokens"], 40)
        self.assertEqual(bucket["unknown"], 1)

    def test_unknown_calls_are_logged(self) -> None:
        with self.assertLogs(token_usage.logger.name, level="INFO") as logs:
            record_usage(UsageRecord("implementer", "claude", None, TODAY), self.path)
        self.assertIn("implementer/claude reported no count", logs.output[0])

    def test_a_new_day_gets_its_own_bucket(self) -> None:
        record_usage(UsageRecord("planner", "codex", 1, _day(1)), self.path)
        record_usage(UsageRecord("planner", "codex", 2, TODAY), self.path)
        days = self._read()["days"]
        self.assertEqual(sorted(days), [_day(1), TODAY])


class PruningTests(StoreTestCase):
    def test_keeps_ninety_days_including_today_and_drops_older(self) -> None:
        self.assertEqual(RETENTION_DAYS, 90)
        self._write(
            {
                _day(90): {"planner": {"codex": _bucket(1)}},
                _day(89): {"planner": {"codex": _bucket(1)}},
                _day(1): {"planner": {"codex": _bucket(1)}},
            }
        )
        record_usage(UsageRecord("planner", "codex", 5, TODAY), self.path)
        self.assertEqual(sorted(self._read()["days"]), [_day(89), _day(1), TODAY])

    def test_the_store_never_grows_past_the_window(self) -> None:
        for offset in range(120, -1, -1):
            record_usage(UsageRecord("planner", "codex", 1, _day(offset)), self.path)
        kept = self._read()["days"]
        self.assertEqual(len(kept), RETENTION_DAYS)
        self.assertEqual(min(kept), _day(RETENTION_DAYS - 1))
        self.assertEqual(max(kept), _day(0))


class AtomicWriteTests(StoreTestCase):
    def test_store_is_owner_only_and_leaves_no_temporary_files(self) -> None:
        record_usage(UsageRecord("planner", "codex", 1, TODAY), self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        leftovers = [
            name
            for name in os.listdir(self.dir)
            if name.endswith(".tmp") or name.startswith(".")
        ]
        self.assertEqual(leftovers, [])

    def test_failed_replace_keeps_the_previous_store(self) -> None:
        record_usage(UsageRecord("planner", "codex", 10, TODAY), self.path)
        before = self.path.read_text()
        with patch(
            "ai_agent.bot.atomic_file.os.replace", side_effect=OSError("disk full")
        ):
            saved = record_usage(UsageRecord("planner", "codex", 99, TODAY), self.path)
        self.assertFalse(saved)
        self.assertEqual(self.path.read_text(), before)

    def test_corrupt_store_is_started_again_with_a_warning(self) -> None:
        self.path.write_text("{not json")
        with self.assertLogs(token_usage.logger.name, level="WARNING"):
            saved = record_usage(UsageRecord("planner", "codex", 3, TODAY), self.path)
        self.assertTrue(saved)
        self.assertEqual(self._read()["days"][TODAY]["planner"]["codex"]["tokens"], 3)

    def test_unwritable_location_returns_false_and_does_not_raise(self) -> None:
        blocker = self.dir / "file"
        blocker.write_text("x")
        saved = record_usage(
            UsageRecord("planner", "codex", 1, TODAY), blocker / "token-usage.json"
        )
        self.assertFalse(saved)


class SnapshotSectionTests(StoreTestCase):
    def test_missing_store_omits_the_section_and_logs_why(self) -> None:
        with self.assertLogs(token_usage.logger.name, level="INFO") as logs:
            self.assertIsNone(usage_section(self.path, today=TODAY))
        self.assertIn("no usage recorded yet", logs.output[0])

    def test_unreadable_store_omits_the_section_and_logs_why(self) -> None:
        self.path.write_text("{not json")
        with self.assertLogs(token_usage.logger.name, level="WARNING") as logs:
            self.assertIsNone(usage_section(self.path, today=TODAY))
        self.assertIn("store unreadable", logs.output[0])

    def test_unknown_format_is_treated_as_unreadable(self) -> None:
        self.path.write_text(json.dumps({"format": 99, "days": {}}))
        self.assertIsNone(usage_section(self.path, today=TODAY))

    def test_omission_is_logged_once_per_reason_not_every_tick(self) -> None:
        with self.assertLogs(token_usage.logger.name, level="INFO") as logs:
            usage_section(self.path, today=TODAY)
            usage_section(self.path, today=TODAY)
        self.assertEqual(len(logs.output), 1)

    def test_section_has_only_dates_roles_tools_and_counts(self) -> None:
        record_usage(UsageRecord("planner", "codex", 120, TODAY), self.path)
        record_usage(UsageRecord("implementer", "claude", None, TODAY), self.path)
        section = usage_section(self.path, today=TODAY)

        self.assertEqual(set(section), {"window_days", "days"})
        self.assertEqual(section["window_days"], 90)
        self.assertEqual(len(section["days"]), 1)
        day = section["days"][0]
        self.assertEqual(set(day), {"date", "usage"})
        self.assertEqual(day["date"], TODAY)
        self.assertEqual(
            day["usage"],
            [
                {
                    "role": "planner",
                    "tool": "codex",
                    "calls": 1,
                    "unknown": 0,
                    "tokens": 120,
                },
                {
                    "role": "implementer",
                    "tool": "claude",
                    "calls": 1,
                    "unknown": 1,
                    "tokens": 0,
                },
            ],
        )
        for row in day["usage"]:
            self.assertEqual(set(row), {"role", "tool", "calls", "unknown", "tokens"})
            for name in ("calls", "unknown", "tokens"):
                self.assertIsInstance(row[name], int)

    def test_section_leaves_out_days_outside_the_window(self) -> None:
        self._write(
            {
                _day(90): {"planner": {"codex": _bucket(7)}},
                _day(3): {"planner": {"codex": _bucket(7)}},
            }
        )
        section = usage_section(self.path, today=TODAY)
        self.assertEqual([day["date"] for day in section["days"]], [_day(3)])

    def test_section_contains_no_extra_or_prompt_text(self) -> None:
        record_usage(UsageRecord("planner", "codex", 9, TODAY), self.path)
        text = json.dumps(usage_section(self.path, today=TODAY))
        for forbidden in ("prompt", "SECRET", "branch", "feature", "/"):
            self.assertNotIn(forbidden, text)

    def test_store_with_injected_fields_is_treated_as_unreadable(self) -> None:
        self._write(
            {TODAY: {"planner": {"codex": {**_bucket(1), "prompt": "SECRET"}}}}
        )
        self.assertIsNone(usage_section(self.path, today=TODAY))


class ConfigDefaultTests(unittest.TestCase):
    def test_default_path_is_under_the_state_directory(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "TOKEN_USAGE_PATH"}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from ai_agent.config import TOKEN_USAGE_PATH; print(TOKEN_USAGE_PATH)",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            result.stdout.strip(), "/var/lib/ai-coding-agent/token-usage.json"
        )


def _bucket(tokens: int) -> dict:
    return {"calls": 1, "unknown": 0, "tokens": tokens}


class DayHelperTests(unittest.TestCase):
    def test_utc_day_uses_the_utc_calendar_day(self) -> None:
        moment = datetime(2026, 10, 21, 23, 59, tzinfo=UTC)
        self.assertEqual(token_usage.utc_day(moment), "2026-10-21")


if __name__ == "__main__":
    unittest.main()
