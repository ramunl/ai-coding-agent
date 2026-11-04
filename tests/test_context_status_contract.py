"""Pin the v1 context-status contract documented in docs/context_status.md.

The field table, safe-reference rules and legacy defaults in that document are
the single source of truth; this module pins them to the implementation so
doc/code drift fails CI. Management (context_management.py, context_controls.py)
and inbox (inbox.py) contracts are intentionally out of scope here.
"""

import json
import typing

from ai_agent.context_models import PacketReference
from ai_agent.context_status import (
    BLOCKER_CODES,
    FRESHNESS_SECONDS,
    MAX_COUNT,
    MAX_PACKET_REFS,
    MAX_TIMESTAMP,
    BlockerCode,
    Freshness,
    Health,
    PacketMetadata,
    SafePacketReference,
    context_status,
)

HEALTH_VALUES = {"healthy", "degraded", "blocked", "unknown", "unavailable"}
FRESHNESS_VALUES = {"fresh", "stale", "unknown"}
BLOCKER_VALUES = {
    "dependency_unknown",
    "dependency_unavailable",
    "dependency_changed",
    "dependency_recovery_pending",
}

REF = PacketReference("packet", "a" * 64)


def test_context_status_field_set_is_exact():
    """to_dict projects exactly the eight documented fields, including nulls."""
    status = context_status((REF,), PacketMetadata(100, True, 0, 0), now=100)
    assert set(status.to_dict()) == {
        "schema_version",
        "health",
        "packet_refs",
        "freshness",
        "observed_at",
        "omission_count",
        "truncation_count",
        "dependency_blockers",
    }
    # Legacy projection keeps every field present even when nothing is known.
    unknown = context_status(now=100).to_dict()
    assert set(unknown) == set(status.to_dict())
    assert unknown["observed_at"] is None
    assert unknown["omission_count"] is None
    assert unknown["truncation_count"] is None


def test_sidecar_and_safe_reference_field_sets_are_exact():
    assert set(PacketMetadata().to_dict()) == {
        "schema_version",
        "observed_at",
        "verified",
        "omission_count",
        "truncation_count",
    }
    assert set(SafePacketReference("packet", "a" * 64).to_dict()) == {
        "packet_id",
        "sha256",
    }


def test_enum_value_sets_are_closed():
    assert set(typing.get_args(Health)) == HEALTH_VALUES
    assert set(typing.get_args(Freshness)) == FRESHNESS_VALUES
    assert set(typing.get_args(BlockerCode)) == BLOCKER_VALUES
    assert BLOCKER_CODES == frozenset(BLOCKER_VALUES)


def test_documented_bounds_are_pinned():
    assert MAX_COUNT == 65_535
    assert MAX_PACKET_REFS == 8
    assert MAX_TIMESTAMP == 253_402_300_799
    assert FRESHNESS_SECONDS == 60


def test_schema_version_is_v1_only():
    assert PacketMetadata().to_dict()["schema_version"] == 1
    assert context_status(now=100).to_dict()["schema_version"] == 1
    # Missing, non-integer and future versions all fall back to the legacy
    # default without raising or migrating.
    for value in (
        None,
        {},
        {"schema_version": 2},
        {"schema_version": "1"},
        {"schema_version": 0},
        {"schema_version": 1.0},
    ):
        assert PacketMetadata.from_dict(value) == PacketMetadata()


def test_legacy_missing_metadata_projects_unknown_nulls():
    for value in (None, {}, {"schema_version": 2}, {"schema_version": "1"}):
        metadata = PacketMetadata.from_dict(value)
        status = context_status((REF,), metadata, now=100)
        assert status.health == status.freshness == "unknown"
        projected = status.to_dict()
        assert projected["observed_at"] is None
        assert projected["omission_count"] is None
        assert projected["truncation_count"] is None
        assert projected["packet_refs"] == [
            {"packet_id": "packet", "sha256": "a" * 64}
        ]
    bare = context_status(now=100).to_dict()
    assert bare["health"] == bare["freshness"] == "unknown"
    assert bare["packet_refs"] == []
    assert bare["dependency_blockers"] == []


def test_canonical_json_bytes_are_pinned():
    status = context_status((REF,), PacketMetadata(100, True, 1, 2), now=100)
    sha = "a" * 64
    expected = (
        '{"dependency_blockers":[],"freshness":"fresh","health":"degraded",'
        '"observed_at":100,"omission_count":1,'
        f'"packet_refs":[{{"packet_id":"packet","sha256":"{sha}"}}],'
        '"schema_version":1,"truncation_count":2}'
    )
    assert status.to_json() == expected
    assert status.to_json().isascii()
    # The same inputs and explicit evaluation time yield the same bytes.
    again = context_status((REF,), PacketMetadata(100, True, 1, 2), now=100)
    assert again.to_json() == status.to_json()
    # Sorted, compact separators exactly match a canonical re-dump.
    assert status.to_json() == json.dumps(
        status.to_dict(), sort_keys=True, separators=(",", ":")
    )
