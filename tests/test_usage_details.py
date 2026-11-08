"""Versioned per-call usage details: schema, bounded store and secret-free section."""

import json
from datetime import date, timedelta

import pytest

from ai_agent import usage_details
from ai_agent.usage_details import (
    FORMAT,
    UsageDetail,
    bounded_output_chars,
    detail_section,
    record_detail,
)

TODAY = "2026-10-21"
TODAY_DATE = date(2026, 10, 21)


def _day(offset: int) -> str:
    return (TODAY_DATE - timedelta(days=offset)).isoformat()


def _detail(**overrides) -> UsageDetail:
    values = {
        "role": "planner",
        "runner": "claude-api",
        "provider": "claude",
        "model": "claude-sonnet-4-6",
        "day": TODAY,
        "attempt_id": "attempt-1",
        "call_id": "call-1",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    values.update(overrides)
    return UsageDetail(**values)


def test_total_never_double_counts_cache_or_reasoning() -> None:
    detail = _detail(
        cache_read_tokens=99,
        cache_creation_tokens=50,
        reasoning_tokens=7,
    )
    assert detail.total_tokens == 15
    assert detail.cache_read_tokens == 99
    assert detail.cache_creation_tokens == 50
    assert detail.reasoning_tokens == 7


def test_total_must_equal_input_plus_output_when_both_are_known() -> None:
    with pytest.raises(ValueError):
        _detail(total_tokens=16)


def test_missing_fields_stay_none_never_zero() -> None:
    detail = _detail(
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cache_read_tokens=None,
        cost=None,
    )
    assert detail.input_tokens is None
    assert detail.output_tokens is None
    assert detail.total_tokens is None


def test_rejects_values_outside_the_vocabulary() -> None:
    for overrides in (
        {"role": "ops"},
        {"runner": "gemini"},
        {"provider": "gemini"},
        {"total_tokens": -1},
        {"cost": -0.01},
        {"duration_seconds": -1},
        {"output_chars": True},
        {"model": "x" * 201},
        {"attempt_id": "bad\nid"},
        {"day": "2026-13-01"},
    ):
        with pytest.raises(ValueError):
            _detail(**overrides)


def test_cost_accepts_tool_reported_values_only() -> None:
    assert _detail(cost=0.0).cost == 0.0
    assert _detail(cost=1.25).cost == 1.25


def test_bounded_output_chars_never_returns_text() -> None:
    assert bounded_output_chars(None) is None
    assert bounded_output_chars("x" * 20) == 20
    assert bounded_output_chars("x" * (usage_details.MAX_OUTPUT_CHARS + 5)) == (
        usage_details.MAX_OUTPUT_CHARS
    )


class TestStore:
    def test_records_are_written_under_format_one(self, tmp_path) -> None:
        path = tmp_path / "usage-details.json"
        assert record_detail(_detail(), path)
        value = json.loads(path.read_text())
        assert value["format"] == FORMAT
        assert len(value["records"]) == 1

    def test_duplicate_attempt_and_call_id_is_not_counted_twice(self, tmp_path) -> None:
        path = tmp_path / "usage-details.json"
        assert record_detail(_detail(), path)
        assert not record_detail(_detail(), path)
        assert len(json.loads(path.read_text())["records"]) == 1

    def test_calls_without_ids_are_not_deduplicated(self, tmp_path) -> None:
        path = tmp_path / "usage-details.json"
        assert record_detail(_detail(attempt_id=None, call_id=None), path)
        assert record_detail(_detail(attempt_id=None, call_id=None), path)
        assert len(json.loads(path.read_text())["records"]) == 2

    def test_record_cap_keeps_only_the_newest(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(usage_details, "USAGE_DETAILS_MAX_RECORDS", 3)
        path = tmp_path / "usage-details.json"
        for index in range(5):
            record_detail(_detail(call_id=f"call-{index}", output_chars=index), path)
        records = json.loads(path.read_text())["records"]
        assert len(records) == 3
        assert [record["output_chars"] for record in records] == [2, 3, 4]

    def test_retention_drops_days_outside_the_window(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(usage_details, "USAGE_DETAILS_RETENTION_DAYS", 90)
        path = tmp_path / "usage-details.json"
        record_detail(_detail(day=_day(90), call_id="old"), path)
        record_detail(_detail(day=TODAY, call_id="new"), path)
        days = [record["day"] for record in json.loads(path.read_text())["records"]]
        assert days == [TODAY]

    def test_corrupt_store_is_started_again(self, tmp_path) -> None:
        path = tmp_path / "usage-details.json"
        path.write_text("{not json")
        assert record_detail(_detail(), path)
        assert len(json.loads(path.read_text())["records"]) == 1

    def test_unwritable_location_returns_false_without_raising(self, tmp_path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        assert not record_detail(_detail(), blocker / "usage-details.json")

    def test_injected_fields_make_the_store_unreadable(self, tmp_path) -> None:
        path = tmp_path / "usage-details.json"
        assert record_detail(_detail(), path)
        value = json.loads(path.read_text())
        value["records"][0]["prompt"] = "SECRET"
        path.write_text(json.dumps(value))
        assert detail_section(path, today=TODAY) is None


class TestSection:
    def test_missing_store_omits_the_section(self, tmp_path) -> None:
        assert detail_section(tmp_path / "missing.json", today=TODAY) is None

    def test_section_excludes_attempt_and_call_ids(self, tmp_path) -> None:
        path = tmp_path / "usage-details.json"
        record_detail(_detail(attempt_id="secret-attempt", call_id="secret-call"), path)
        section = detail_section(path, today=TODAY)
        text = json.dumps(section)
        assert "secret-attempt" not in text
        assert "secret-call" not in text
        assert "attempt_id" not in text
        assert "call_id" not in text
        assert set(section) == {"window_days", "records"}

    def test_section_leaves_out_days_outside_the_window(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(usage_details, "USAGE_DETAILS_RETENTION_DAYS", 90)
        path = tmp_path / "usage-details.json"
        record_detail(_detail(day=_day(90), call_id="old"), path)
        record_detail(_detail(day=_day(3), call_id="kept"), path)
        section = detail_section(path, today=TODAY)
        assert [record["day"] for record in section["records"]] == [_day(3)]
