"""Offline DS16 benchmark fixture and evidence-gate acceptance/rejection."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_agent.ds16_benchmark import (
    CASE_NAMES,
    EVIDENCE_RECORD_SCHEMA,
    TASK_FIXTURES,
    build_fixture_record,
    required_outcomes,
)
from ai_agent.ds16_evidence import validate

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scripts" / "ds16_benchmark_cli.py"


@pytest.fixture
def record():
    return build_fixture_record(
        "source_heavy_revision",
        run_id="ds16-test-run",
        commit="a" * 40,
        started_at="2026-11-11T10:00:00Z",
        finished_at="2026-11-11T10:10:00Z",
    )


def _mutate_measurement(record, name, **changes):
    record["measurements"][name].update(changes)
    return record


def test_valid_record_passes(record):
    assert validate(record) == {"valid": True, "errors": []}


def test_five_fixtures_are_defined_and_produce_valid_records():
    assert tuple(TASK_FIXTURES) == CASE_NAMES
    for case in CASE_NAMES:
        record = build_fixture_record(
            case,
            run_id="ds16-test-run",
            commit="a" * 40,
            started_at="2026-11-11T10:00:00Z",
            finished_at="2026-11-11T10:10:00Z",
        )
        assert validate(record)["valid"], case


def test_every_fixture_requires_all_common_outcomes():
    for case in CASE_NAMES:
        outcomes = required_outcomes(case)
        for key in (
            "build_exit_code",
            "orphan_workers",
            "lost_history_entries",
            "worker_count",
            "stop_controls_preserved",
            "unlimited_runtime_preserved",
        ):
            assert key in outcomes, (case, key)


def test_schema_lists_required_record_fields():
    for key in (
        "benchmark_version",
        "case",
        "evidence_kind",
        "side_effect_free",
        "run_id",
        "repository",
        "commit",
        "started_at",
        "finished_at",
        "outcomes",
        "measurements",
        "evidence",
    ):
        assert key in EVIDENCE_RECORD_SCHEMA["required"]


def test_nonobject_record_rejected():
    assert validate(None)["valid"] is False


def test_corrections_reject_non_integer_and_negative(record):
    for value in (0.5, -1, True, "0", None, 10**400):
        mutated = copy.deepcopy(record)
        _mutate_measurement(mutated, "corrections", value=value)
        assert validate(mutated)["valid"] is False


def test_memory_rejects_negative_and_over_limit(record):
    for value in (-1, 257):
        mutated = copy.deepcopy(record)
        _mutate_measurement(mutated, "peak_memory", value=value)
        assert validate(mutated)["valid"] is False
    over_limit = _mutate_measurement(copy.deepcopy(record), "peak_memory", value=257)
    assert "exceeds memory limit" in validate(over_limit)["errors"][0]


def test_memory_respects_custom_limit(record):
    assert validate(record, memory_limit=64)["valid"] is False


def test_time_rejects_negative_and_wrong_unit(record):
    negative = _mutate_measurement(copy.deepcopy(record), "elapsed_time", value=-1)
    assert validate(negative)["valid"] is False
    wrong_unit = _mutate_measurement(copy.deepcopy(record), "elapsed_time", unit="ms")
    assert validate(wrong_unit)["valid"] is False


def test_cost_rejects_negative(record):
    for name in ("actual_cost", "estimated_cost"):
        mutated = _mutate_measurement(copy.deepcopy(record), name, value=-1)
        assert validate(mutated)["valid"] is False


def test_stop_controls_required(record):
    mutated = copy.deepcopy(record)
    mutated["outcomes"]["stop_controls_preserved"]["value"] = False
    assert validate(mutated)["valid"] is False
    mutated = copy.deepcopy(record)
    del mutated["outcomes"]["unlimited_runtime_preserved"]
    assert validate(mutated)["valid"] is False


def test_orphan_workers_and_lost_history_must_be_zero(record):
    for key in ("orphan_workers", "lost_history_entries"):
        mutated = copy.deepcopy(record)
        mutated["outcomes"][key]["value"] = 1
        assert validate(mutated)["valid"] is False


def test_false_success_flags_rejected(record):
    mutated = copy.deepcopy(record)
    mutated["success"] = True
    mutated["status"] = "passed"
    result = validate(mutated)
    assert result["valid"] is False
    assert any("success" in error for error in result["errors"])
    assert any("status" in error for error in result["errors"])


def test_identity_and_envelope_checks(record):
    mutated = copy.deepcopy(record)
    mutated["case"] = "not-a-case"
    assert validate(mutated)["valid"] is False
    mutated = copy.deepcopy(record)
    mutated["commit"] = "z" * 40
    assert validate(mutated)["valid"] is False
    mutated = copy.deepcopy(record)
    mutated["side_effect_free"] = False
    assert validate(mutated)["valid"] is False


def test_pending_evidence_rejected(record):
    mutated = copy.deepcopy(record)
    mutated["evidence"][0]["status"] = "pending"
    assert validate(mutated)["valid"] is False


def test_cli_emits_valid_record():
    result = subprocess.run(
        [sys.executable, str(CLI), "--case", "cancellation"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    record = json.loads(result.stdout)
    assert record["case"] == "cancellation"
    assert record["evidence_kind"] == "fixture"
    assert record["side_effect_free"] is True
    assert validate(record)["valid"]


def test_cli_honours_commit_and_timestamps():
    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "--commit",
            "b" * 40,
            "--started-at",
            "2026-11-11T10:00:00Z",
            "--finished-at",
            "2026-11-11T10:10:00Z",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["commit"] == "b" * 40
    assert record["started_at"] == "2026-11-11T10:00:00Z"
    assert record["finished_at"] == "2026-11-11T10:10:00Z"


def test_cli_rejects_unknown_case():
    result = subprocess.run(
        [sys.executable, str(CLI), "--case", "nope"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 2
