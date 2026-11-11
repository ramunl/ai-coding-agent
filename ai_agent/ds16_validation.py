"""Bounded offline DS16 evidence gate: python -m ai_agent.ds16_validation FILE."""

import argparse
import json
import os
import stat
from pathlib import Path

from ai_agent.ds16_contract import (
    CASE_OUTCOMES,
    MAX_BYTES,
    MEASUREMENTS,
    VERSION,
    Contract,
)


def _identity(check: Contract) -> None:
    data = check.document
    check.require(
        type(data.get("schema_version")) is int and data["schema_version"] == VERSION,
        "schema_version",
        "required version 1",
    )
    for key in ("run_id", "repository"):
        check.text(data.get(key), key)
    check.digest(data.get("commit"), "commit", 40)
    check.require(
        data.get("evidence_kind") in ("fixture", "production"),
        "evidence_kind",
        "required fixture or production",
    )
    for section, keys in {
        "provider_route": ("provider", "model", "route"),
        "environment": ("host", "os", "runtime", "collector"),
    }.items():
        record = check.obj(data.get(section), section)
        for key in keys:
            check.text(record.get(key), section + "." + key)
    route = check.obj(data.get("provider_route"), "provider_route")
    check.require(
        route.get("native_anthropic_status")
        in ("available", "unavailable", "not_tested"),
        "provider_route.native_anthropic_status",
        "record availability separately",
    )
    check.text(
        route.get("native_anthropic_note"), "provider_route.native_anthropic_note"
    )
    start = check.timestamp(data.get("started_at"), "started_at")
    end = check.timestamp(data.get("finished_at"), "finished_at")
    check.require(
        bool(start and end and start <= end),
        "finished_at",
        "must not precede started_at",
    )
    check.evidence(start, end)


def _cases(check: Contract, memory_limit: float | None) -> None:
    cases = check.document.get("cases")
    check.require(
        isinstance(cases, list) and len(cases) == 5,
        "cases",
        "required exactly five distinct cases",
    )
    seen = set()
    for index, item in enumerate(cases[:5] if isinstance(cases, list) else []):
        path = f"cases[{index}]"
        case = check.obj(item, path)
        name = case.get("case")
        valid = isinstance(name, str) and name in CASE_OUTCOMES
        check.require(valid, path + ".case", "required DS16 case name")
        if not valid:
            continue
        check.require(name not in seen, path + ".case", "duplicate case")
        seen.add(name)
        outcomes = check.obj(case.get("outcomes"), path + ".outcomes")
        expected = dict(
            CASE_OUTCOMES[name],
            build_exit_code=0,
            orphan_workers=0,
            lost_history_entries=0,
            worker_count=1,
        )
        for key, value in expected.items():
            check.observation(outcomes.get(key), path + ".outcomes." + key, value)
        metrics = check.obj(case.get("measurements"), path + ".measurements")
        for key, unit in MEASUREMENTS.items():
            field = path + ".measurements." + key
            number = check.measurement(metrics.get(key), field, unit)
            if number is None:
                continue
            if key == "completion":
                check.require(
                    number == 1, field + ".value", "case completion must be 1"
                )
            if key == "corrections":
                check.require(
                    number == int(number), field + ".value", "required integer count"
                )
            if key == "peak_memory":
                check.require(
                    memory_limit is not None and number <= memory_limit,
                    field + ".value",
                    "exceeds recorded one-worker memory limit",
                )
    check.require(seen == set(CASE_OUTCOMES), "cases", "all five DS16 cases required")


def _rollback(check: Contract) -> None:
    rollback = check.obj(check.document.get("rollback"), "rollback")
    for key, role in {
        "backup_ref": "backup",
        "release_ref": "release",
        "execution_ref": "codex_rollback",
    }.items():
        check.reference(rollback.get(key), "rollback." + key, role)
    check.require(
        rollback.get("run_id") == check.document.get("run_id"),
        "rollback.run_id",
        "must match run identity",
    )
    check.require(
        rollback.get("target") == "codex", "rollback.target", "required codex"
    )
    check.require(
        rollback.get("status") == "verified",
        "rollback.status",
        "required verified rollback execution",
    )
    for key in ("state", "config", "release"):
        record = check.obj(rollback.get(key), "rollback." + key)
        for field in ("expected_sha256", "restored_sha256"):
            check.digest(record.get(field), f"rollback.{key}.{field}")
        check.require(
            record.get("expected_sha256") == record.get("restored_sha256"),
            "rollback." + key,
            "restored digest must match backup/release",
        )
        check.reference(record.get("evidence_ref"), f"rollback.{key}.evidence_ref")
    completed = check.timestamp(rollback.get("completed_at"), "rollback.completed_at")
    probes = check.obj(
        rollback.get("post_rollback_probes"), "rollback.post_rollback_probes"
    )
    for key, expected in {
        "codex_route": "codex",
        "build_exit_code": 0,
        "orphan_workers": 0,
        "lost_history_entries": 0,
    }.items():
        check.observation(
            probes.get(key), "rollback.post_rollback_probes." + key, expected
        )
        probe = probes.get(key)
        ref = probe.get("evidence_ref") if isinstance(probe, dict) else None
        records = check.document.get("evidence")
        for record in records if isinstance(records, list) else []:
            if isinstance(record, dict) and record.get("id") == ref:
                captured = check.timestamp(
                    record.get("captured_at"), "rollback.probe_time"
                )
                check.require(
                    bool(completed and captured and captured > completed),
                    "rollback.post_rollback_probes." + key,
                    "probe must follow rollback",
                )
    started = check.timestamp(check.document.get("started_at"), "started_at")
    check.require(
        bool(started and completed and completed >= started),
        "rollback.completed_at",
        "must fall within run before probes",
    )


def validate(document: object) -> dict:
    """Derive contract status; even production records require Ops acceptance."""
    check = Contract(document if isinstance(document, dict) else {})
    check.require(isinstance(document, dict), "$", "required JSON object")
    _identity(check)
    policy = check.obj(check.document.get("policy"), "policy")
    check.text(policy.get("measurement_methodology"), "policy.measurement_methodology")
    check.text(policy.get("memory_scope"), "policy.memory_scope")
    check.observation(policy.get("worker_count"), "policy.worker_count", 1)
    limit = check.measurement(policy.get("memory_limit"), "policy.memory_limit", "MiB")
    check.require(
        limit is not None and limit > 0,
        "policy.memory_limit.value",
        "explicit positive one-worker limit required; no default",
    )
    _cases(check, limit)
    _rollback(check)
    return {
        "schema_version": VERSION,
        "status": "rejected"
        if check.errors
        else (
            "fixture_contract_pass"
            if check.document.get("evidence_kind") == "fixture"
            else "production_contract_pass"
        ),
        "production_acceptance": "pending_ops",
        "errors": check.errors,
    }


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant")


def read_document(path: Path) -> object:
    """Read only a bounded regular JSON file; never resolve evidence references."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("expected regular file of at most 262144 bytes")
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("input exceeds byte limit")
    document = json.loads(
        payload, object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    pending = [(document, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 20:
            raise ValueError("JSON nesting exceeds 20 levels")
        children = value.values() if isinstance(value, dict) else value
        if isinstance(value, (dict, list)):
            pending.extend((child, depth + 1) for child in children)
    return document


def main() -> int:
    """Print bounded JSON diagnostics and return nonzero on rejection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        result = validate(read_document(args.file))
    except (OSError, ValueError, RecursionError):
        result = {
            "status": "rejected",
            "production_acceptance": "pending_ops",
            "errors": [
                "$: unreadable or invalid JSON; require a regular file <=262144 bytes, "
                "unique keys and depth <=20"
            ],
        }
    print(json.dumps(result))
    return int(result["status"] == "rejected")


if __name__ == "__main__":
    raise SystemExit(main())
