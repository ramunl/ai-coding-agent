"""Bounded per-record DS16 benchmark evidence validators.

Each validator checks one dimension of a single fixture evidence record and
appends bounded, field-oriented errors without echoing input values. A fixture
record passing these checks is internally conformant only: it never proves
production acceptance, which remains Ops-controlled. Stop, orphan-worker,
lost-history and false-success checks exercise the gate mechanics locally but
require Ops-provided evidence for a real integration run.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from ai_agent.ds16_benchmark import (
    BENCHMARK_VERSION,
    CASE_NAMES,
    FIXTURE_MEMORY_LIMIT_MIB,
)

Errors = list[str]


def _finite_nonnegative(value: object, path: str, errors: Errors) -> float | None:
    """Require a finite nonnegative number and return it as a float."""
    valid = type(value) in (int, float)
    number: float | None = None
    try:
        valid = valid and math.isfinite(value) and value >= 0
        number = float(value) if valid else None
    except (OverflowError, ValueError):
        valid = False
        number = None
    if not valid:
        errors.append(f"{path}: required finite nonnegative number")
        return None
    return number


def _observation(
    record: dict[str, Any], key: str, expected: object, path: str, errors: Errors
) -> None:
    """Check an exact-typed outcome observation and its evidence reference."""
    outcomes = record.get("outcomes")
    if not isinstance(outcomes, dict):
        errors.append(f"{path}: missing outcomes object")
        return
    item = outcomes.get(key)
    if not isinstance(item, dict):
        errors.append(f"{path}: required observation object")
        return
    reference = item.get("evidence_ref")
    if not (isinstance(reference, str) and reference.strip()):
        errors.append(f"{path}.evidence_ref: required nonempty text")
    value = item.get("value")
    if type(value) is not type(expected) or value != expected:
        errors.append(f"{path}.value: required outcome {expected!r}")


def _measurement(
    record: dict[str, Any], name: str, unit: str, path: str, errors: Errors
) -> float | None:
    """Check one measurement's unit, source, reference and numeric value."""
    measurements = record.get("measurements")
    if not isinstance(measurements, dict):
        errors.append(f"{path}.{name}: missing measurements object")
        return None
    item = measurements.get(name)
    if not isinstance(item, dict):
        errors.append(f"{path}.{name}: required measurement object")
        return None
    if item.get("unit") != unit:
        errors.append(f"{path}.{name}.unit: required {unit}")
    source = item.get("source")
    if not (isinstance(source, str) and source.strip()):
        errors.append(f"{path}.{name}.source: required nonempty text")
    reference = item.get("evidence_ref")
    if not (isinstance(reference, str) and reference.strip()):
        errors.append(f"{path}.{name}.evidence_ref: required nonempty text")
    return _finite_nonnegative(item.get("value"), f"{path}.{name}.value", errors)


def _iso_timestamp(value: object, path: str, errors: Errors) -> datetime | None:
    """Require an ISO 8601 timestamp with an explicit timezone."""
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed
    except ValueError:
        pass
    errors.append(f"{path}: required ISO 8601 timestamp with timezone")
    return None


def validate_corrections(record: dict[str, Any], errors: Errors) -> None:
    """Require a nonnegative integer intervention count."""
    value = _measurement(record, "corrections", "count", "measurements", errors)
    if value is not None and value != int(value):
        errors.append("measurements.corrections.value: required integer count")


def validate_memory(
    record: dict[str, Any],
    errors: Errors,
    memory_limit: float | None = FIXTURE_MEMORY_LIMIT_MIB,
) -> None:
    """Require peak memory in MiB no greater than the recorded limit."""
    value = _measurement(record, "peak_memory", "MiB", "measurements", errors)
    if value is not None and memory_limit is not None and value > memory_limit:
        errors.append("measurements.peak_memory.value: exceeds memory limit")


def validate_time(record: dict[str, Any], errors: Errors) -> None:
    """Require a nonnegative measured wall time in seconds."""
    _measurement(record, "elapsed_time", "s", "measurements", errors)


def validate_cost(record: dict[str, Any], errors: Errors) -> None:
    """Require nonnegative actual and estimated cost in USD."""
    _measurement(record, "actual_cost", "USD", "measurements", errors)
    _measurement(record, "estimated_cost", "USD", "measurements", errors)


def validate_stop(record: dict[str, Any], errors: Errors) -> None:
    """Require evidence that Stop and unlimited-runtime controls were preserved."""
    for key in ("stop_controls_preserved", "unlimited_runtime_preserved"):
        _observation(record, key, True, f"outcomes.{key}", errors)


def validate_orphan_workers(record: dict[str, Any], errors: Errors) -> None:
    """Require zero orphan workers at case completion."""
    _observation(record, "orphan_workers", 0, "outcomes.orphan_workers", errors)


def validate_lost_history(record: dict[str, Any], errors: Errors) -> None:
    """Require zero lost history entries at case completion."""
    _observation(
        record, "lost_history_entries", 0, "outcomes.lost_history_entries", errors
    )


def validate_false_success(record: dict[str, Any], errors: Errors) -> None:
    """Reject standalone success/status flags; the gate derives success itself."""
    if "success" in record:
        errors.append("$: standalone 'success' flag is not evidence")
    if "status" in record:
        errors.append("$: standalone 'status' flag is not evidence")


def _evidence_entries(
    record: dict[str, Any],
    started: datetime | None,
    finished: datetime | None,
    errors: Errors,
) -> None:
    """Check evidence identity, verification status and capture interval."""
    entries = record.get("evidence")
    if not isinstance(entries, list) or not entries:
        errors.append("evidence: required nonempty list")
        return
    seen: set[str] = set()
    for index, item in enumerate(entries[:16]):
        path = f"evidence[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: required object")
            continue
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier.strip():
            if identifier in seen:
                errors.append(f"{path}.id: duplicate id")
            seen.add(identifier)
        else:
            errors.append(f"{path}.id: required nonempty text")
        if item.get("status") != "verified":
            errors.append(f"{path}.status: required verified")
        uri = item.get("uri")
        if not (isinstance(uri, str) and uri.strip()):
            errors.append(f"{path}.uri: required nonempty text")
        digest = item.get("sha256")
        if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            errors.append(f"{path}.sha256: required 64-character hexadecimal digest")
        captured = _iso_timestamp(
            item.get("captured_at"), f"{path}.captured_at", errors
        )
        if started is not None and finished is not None and captured is not None:
            if not (started <= captured <= finished):
                errors.append(f"{path}.captured_at: must fall within run timestamps")


def validate(
    record: object,
    *,
    memory_limit: float | None = FIXTURE_MEMORY_LIMIT_MIB,
) -> dict[str, Any]:
    """Derive a per-record verdict; a pass never proves production acceptance."""
    errors: Errors = []
    if not isinstance(record, dict):
        return {"valid": False, "errors": ["$: required JSON object"]}
    doc: dict[str, Any] = record
    if doc.get("benchmark_version") != BENCHMARK_VERSION:
        errors.append(f"benchmark_version: required {BENCHMARK_VERSION}")
    if doc.get("case") not in CASE_NAMES:
        errors.append("case: required DS16 benchmark case name")
    if doc.get("evidence_kind") != "fixture":
        errors.append(
            "evidence_kind: required fixture; production evidence is Ops-controlled"
        )
    if doc.get("side_effect_free") is not True:
        errors.append("side_effect_free: required true for fixture records")
    for key in ("run_id", "repository"):
        value = doc.get(key)
        if not (isinstance(value, str) and value.strip()):
            errors.append(f"{key}: required nonempty text")
    commit = doc.get("commit")
    if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)):
        errors.append("commit: required 40-character lowercase hexadecimal digest")
    started = _iso_timestamp(doc.get("started_at"), "started_at", errors)
    finished = _iso_timestamp(doc.get("finished_at"), "finished_at", errors)
    if started is not None and finished is not None and finished < started:
        errors.append("finished_at: must not precede started_at")
    _evidence_entries(doc, started, finished, errors)
    validate_corrections(doc, errors)
    validate_memory(doc, errors, memory_limit)
    validate_time(doc, errors)
    validate_cost(doc, errors)
    validate_stop(doc, errors)
    validate_orphan_workers(doc, errors)
    validate_lost_history(doc, errors)
    validate_false_success(doc, errors)
    return {"valid": not errors, "errors": errors}
