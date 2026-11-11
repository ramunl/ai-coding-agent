"""Offline DS16 contract acceptance and rejection fixtures, collected by CI."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_agent.ds16_contract import MAX_BYTES, MEASUREMENTS
from ai_agent.ds16_validation import read_document, validate

FIXTURES = Path(__file__).parent / "fixtures" / "ds16"


@pytest.fixture
def valid():
    return json.loads((FIXTURES / "valid.json").read_text())


def test_fixture_pass_is_not_production_acceptance(valid):
    result = validate(valid)
    assert result == {
        "schema_version": 1,
        "status": "fixture_contract_pass",
        "production_acceptance": "pending_ops",
        "errors": [],
    }


@pytest.mark.parametrize(
    "fixture", sorted(FIXTURES.glob("*.mutation.json")), ids=lambda p: p.stem
)
def test_rejection_fixtures(valid, fixture):
    mutation = json.loads(fixture.read_text())
    target = valid
    for key in mutation["path"][:-1]:
        target = target[key]
    target[mutation["path"][-1]] = mutation["value"]
    valid["success"] = True
    valid["status"] = "passed"
    result = validate(valid)
    assert result["status"] == "rejected"
    assert any(mutation["error"] in error for error in result["errors"])


@pytest.mark.parametrize("measurement", MEASUREMENTS)
@pytest.mark.parametrize(
    "value", [None, True, -1, "1", float("nan"), float("inf"), float("-inf"), 10**400]
)
def test_invalid_measurements_rejected(valid, measurement, value):
    valid["cases"][0]["measurements"][measurement]["value"] = value
    assert validate(valid)["status"] == "rejected"


@pytest.mark.parametrize("measurement", MEASUREMENTS)
@pytest.mark.parametrize("field", ["value", "unit", "source", "evidence_ref"])
def test_measurement_fields_required(valid, measurement, field):
    del valid["cases"][0]["measurements"][measurement][field]
    assert validate(valid)["status"] == "rejected"


def test_every_case_outcome_is_enforced(valid):
    for index, case in enumerate(valid["cases"]):
        for key in case["outcomes"]:
            mutated = copy.deepcopy(valid)
            mutated["cases"][index]["outcomes"][key]["value"] = None
            assert validate(mutated)["status"] == "rejected", (index, key)


@pytest.mark.parametrize(
    "field",
    ["evidence", "cases", "rollback", "policy", "provider_route", "environment"],
)
@pytest.mark.parametrize("value", [None, False, [], {}, "invalid"])
def test_malformed_sections_rejected_without_crashing(valid, field, value):
    valid[field] = value
    assert validate(valid)["status"] == "rejected"


@pytest.mark.parametrize("value", [None, [], 1, "invalid"])
def test_nonobject_document(value):
    assert validate(value)["status"] == "rejected"


def test_evidence_identity_and_dates_required(valid):
    for field in ("run_id", "commit", "evidence_kind", "sha256", "uri", "captured_at"):
        mutated = copy.deepcopy(valid)
        del mutated["evidence"][0][field]
        assert validate(mutated)["status"] == "rejected"
    valid["finished_at"] = "2026-11-11T09:00:00Z"
    assert validate(valid)["status"] == "rejected"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"a":1,"a":2}',
        b"{",
        b"\xff",
        b'{"a":NaN}',
        b"[" * 22 + b"0" + b"]" * 22,
        b" " * (MAX_BYTES + 1),
    ],
)
def test_bounded_reader_rejects_bad_json(tmp_path, payload):
    path = tmp_path / "input.json"
    path.write_bytes(payload)
    with pytest.raises((ValueError, RecursionError)):
        read_document(path)


def test_reader_rejects_fifo_and_symlink(tmp_path):
    import os

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        read_document(fifo)
    link = tmp_path / "link"
    link.symlink_to(FIXTURES / "valid.json")
    with pytest.raises(OSError):
        read_document(link)


def test_cli_success_and_rejection(tmp_path, valid):
    path = tmp_path / "input.json"
    for payload, code in [(json.dumps(valid), 0), ('{"success": true}', 1), ("{", 1)]:
        path.write_text(payload)
        result = subprocess.run(
            [sys.executable, "-m", "ai_agent.ds16_validation", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == code
        report = json.loads(result.stdout)
        assert report["production_acceptance"] == "pending_ops"
        assert bool(report["errors"]) == bool(code)
        assert not result.stderr


def test_healthy_deploy_reference_does_not_prove_rollback(valid):
    valid["rollback"]["execution_ref"] = "release"
    assert validate(valid)["status"] == "rejected"


def test_production_records_still_need_ops_acceptance(valid):
    valid["evidence_kind"] = "production"
    for record in valid["evidence"]:
        record["evidence_kind"] = "production"
    assert validate(valid)["status"] == "rejected"  # Fixture URIs cannot be promoted.
    for record in valid["evidence"]:
        record["uri"] = "ops-artifact://" + record["id"]
    result = validate(valid)
    assert result["status"] == "production_contract_pass"
    assert result["production_acceptance"] == "pending_ops"
