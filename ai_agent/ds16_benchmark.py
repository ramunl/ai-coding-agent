"""DS16 benchmark task fixtures and the per-run evidence record schema.

This module is offline and side-effect free. It defines the five required
benchmark scenarios and the JSON shape of a single fixture evidence record; it
never runs an implementation, contacts a provider, or mutates the repository.
Real integration runs and production validation are Ops-controlled.
"""

from __future__ import annotations

from typing import Any

from ai_agent.ds16_contract import CASE_OUTCOMES, MEASUREMENTS

BENCHMARK_VERSION = 1
FIXTURE_MEMORY_LIMIT_MIB = 256
SYNTHETIC_SOURCE = "Synthetic side-effect-free fixture; not observed performance"
SYNTHETIC_SHA256 = "c" * 64

CASE_NAMES: tuple[str, ...] = tuple(CASE_OUTCOMES)

COMMON_OUTCOMES: dict[str, object] = {
    "build_exit_code": 0,
    "orphan_workers": 0,
    "lost_history_entries": 0,
    "worker_count": 1,
    "stop_controls_preserved": True,
    "unlimited_runtime_preserved": True,
}

TASK_FIXTURES: dict[str, dict[str, Any]] = {
    "source_heavy_revision": {
        "goal": (
            "Plan and revise a feature spanning at least three distinct source "
            "artifacts, then apply one requested revision."
        ),
        "prompt": (
            "Revise a source-heavy change across three distinct source files "
            "and apply exactly one requested revision."
        ),
        "outcomes": {"sources_used": 3, "revisions_applied": 1},
        "measurements": tuple(MEASUREMENTS),
    },
    "failing_build_repair": {
        "goal": (
            "Introduce a change that fails the build, then repair it so the "
            "build passes."
        ),
        "prompt": (
            "Make a change that fails the build, record the failing command, "
            "then repair the change so the build passes."
        ),
        "outcomes": {"initial_build_exit_code": 1, "repairs_applied": 1},
        "measurements": tuple(MEASUREMENTS),
    },
    "screenshot_delivery": {
        "goal": (
            "Deliver a screenshot through a supported delivery route and "
            "confirm receipt."
        ),
        "prompt": (
            "Deliver a screenshot through a supported route and confirm the "
            "owner actually received it."
        ),
        "outcomes": {"supported": True, "delivered": True},
        "measurements": tuple(MEASUREMENTS),
    },
    "cancellation": {
        "goal": (
            "Start an implementation, request cancellation, and reach a "
            "cancelled terminal state with cleanup."
        ),
        "prompt": (
            "Start an implementation, request cancellation, and confirm the "
            "run reaches a cancelled terminal state with no orphans."
        ),
        "outcomes": {"cancel_requested": True, "terminal_state": "cancelled"},
        "measurements": tuple(MEASUREMENTS),
    },
    "provider_outage": {
        "goal": (
            "Observe a provider outage and confirm the run is handled through "
            "fallback or explicit failure reporting."
        ),
        "prompt": (
            "Run once against an unavailable provider and confirm the outage "
            "is handled through the existing fallback or an explicit failure."
        ),
        "outcomes": {"outage_observed": True, "terminal_state": "handled"},
        "measurements": tuple(MEASUREMENTS),
    },
}

OBSERVATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "evidence_ref"],
    "properties": {
        "value": {
            "description": "Exact typed outcome: boolean, integer, or string",
        },
        "evidence_ref": {"type": "string", "minLength": 1},
    },
}

MEASUREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value", "unit", "source", "evidence_ref"],
    "properties": {
        "value": {"type": "number"},
        "unit": {"type": "string", "minLength": 1},
        "source": {"type": "string", "minLength": 1},
        "evidence_ref": {"type": "string", "minLength": 1},
    },
}

EVIDENCE_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "status", "uri", "sha256", "captured_at", "role"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "status": {"const": "verified"},
        "uri": {"type": "string", "minLength": 1},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "captured_at": {"type": "string", "format": "date-time"},
        "role": {"const": "observation"},
    },
}

EVIDENCE_RECORD_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "DS16 benchmark fixture evidence record",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "benchmark_version",
        "case",
        "evidence_kind",
        "side_effect_free",
        "run_id",
        "repository",
        "commit",
        "started_at",
        "finished_at",
        "ops_note",
        "outcomes",
        "measurements",
        "evidence",
    ],
    "properties": {
        "benchmark_version": {"const": BENCHMARK_VERSION},
        "case": {"enum": list(CASE_NAMES)},
        "evidence_kind": {"const": "fixture"},
        "side_effect_free": {"const": True},
        "run_id": {"type": "string", "minLength": 1},
        "repository": {"type": "string", "minLength": 1},
        "commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "started_at": {"type": "string", "format": "date-time"},
        "finished_at": {"type": "string", "format": "date-time"},
        "ops_note": {"type": "string", "minLength": 1},
        "outcomes": {
            "type": "object",
            "additionalProperties": OBSERVATION_SCHEMA,
            "required": list(COMMON_OUTCOMES),
        },
        "measurements": {
            "type": "object",
            "additionalProperties": MEASUREMENT_SCHEMA,
            "required": list(MEASUREMENTS),
            "properties": {name: MEASUREMENT_SCHEMA for name in MEASUREMENTS},
        },
        "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": EVIDENCE_ENTRY_SCHEMA,
        },
    },
}


def required_outcomes(case: str) -> dict[str, object]:
    """Return complete outcome expectations for one benchmark case."""
    if case not in TASK_FIXTURES:
        raise KeyError(f"unknown DS16 benchmark case: {case}")
    outcomes: dict[str, object] = dict(COMMON_OUTCOMES)
    outcomes.update(TASK_FIXTURES[case]["outcomes"])
    return outcomes


def build_fixture_record(
    case: str,
    *,
    run_id: str,
    commit: str,
    started_at: str,
    finished_at: str,
    repository: str = "ai-coding-agent",
) -> dict[str, Any]:
    """Return a complete side-effect-free synthetic evidence record for one case.

    Values are synthetic and demonstrate the record shape; they are not
    performance evidence and never prove production acceptance.
    """
    if case not in TASK_FIXTURES:
        raise KeyError(f"unknown DS16 benchmark case: {case}")
    outcomes: dict[str, dict[str, object]] = {
        key: {"value": value, "evidence_ref": "fixture-log"}
        for key, value in required_outcomes(case).items()
    }
    measurements: dict[str, dict[str, object]] = {
        "completion": {
            "value": 1,
            "unit": "ratio",
            "source": SYNTHETIC_SOURCE,
            "evidence_ref": "fixture-log",
        },
        "corrections": {
            "value": 0,
            "unit": "count",
            "source": SYNTHETIC_SOURCE,
            "evidence_ref": "fixture-log",
        },
        "peak_memory": {
            "value": 128,
            "unit": "MiB",
            "source": SYNTHETIC_SOURCE,
            "evidence_ref": "fixture-log",
        },
        "elapsed_time": {
            "value": 10,
            "unit": "s",
            "source": SYNTHETIC_SOURCE,
            "evidence_ref": "fixture-log",
        },
        "actual_cost": {
            "value": 0.02,
            "unit": "USD",
            "source": SYNTHETIC_SOURCE,
            "evidence_ref": "fixture-log",
        },
        "estimated_cost": {
            "value": 0.03,
            "unit": "USD",
            "source": SYNTHETIC_SOURCE,
            "evidence_ref": "fixture-log",
        },
    }
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "case": case,
        "evidence_kind": "fixture",
        "side_effect_free": True,
        "run_id": run_id,
        "repository": repository,
        "commit": commit,
        "started_at": started_at,
        "finished_at": finished_at,
        "ops_note": (
            "Synthetic side-effect-free fixture record; real DS16 integration "
            "runs and production acceptance are Ops-controlled."
        ),
        "outcomes": outcomes,
        "measurements": measurements,
        "evidence": [
            {
                "id": "fixture-log",
                "status": "verified",
                "uri": "fixture://ds16-benchmark-log",
                "sha256": SYNTHETIC_SHA256,
                "captured_at": finished_at,
                "role": "observation",
            }
        ],
    }
