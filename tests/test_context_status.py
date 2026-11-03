"""Public context contract boundaries and conservative legacy semantics."""

import json
from dataclasses import asdict, replace

import pytest

from ai_agent.context_models import (
    PacketReference,
    RecordReference,
    RepositoryIdentity,
    Scope,
)
from ai_agent.context_status import (
    MAX_COUNT,
    MAX_PACKET_REFS,
    MAX_TIMESTAMP,
    PacketMetadata,
    context_status,
    dependency_blockers,
    freshness,
    safe_packet_ref,
)

REF = PacketReference("packet", "a" * 64)
META = PacketMetadata(100, True, 0, 0)


def test_unknown_legacy_and_failed_access_are_distinct():
    for value in (None, {}, {"schema_version": 2}, {"observed_at": 100}):
        metadata = PacketMetadata.from_dict(value)
        status = context_status((REF,), metadata, now=100).to_dict()
        assert status["health"] == status["freshness"] == "unknown"
        assert status["omission_count"] is None
        assert status["truncation_count"] is None
        assert status["observed_at"] is None
    assert context_status(now=100, access_failed=True).health == "unavailable"


@pytest.mark.parametrize(
    "now,verified,expected",
    [
        (100, True, "fresh"),
        (160, True, "fresh"),
        (161, True, "stale"),
        (99, True, "unknown"),
        (100, False, "unknown"),
        (True, True, "unknown"),
    ],
)
def test_freshness_requires_verified_nonfuture_evidence(now, verified, expected):
    assert freshness(replace(META, verified=verified), now=now) == expected


def test_health_precedence():
    assert context_status((REF,), META, now=100).health == "healthy"
    assert context_status((REF,), META, now=161).health == "degraded"
    assert (
        context_status((REF,), replace(META, omission_count=1), now=100).health
        == "degraded"
    )
    assert (
        context_status((REF,), replace(META, truncation_count=1), now=100).health
        == "degraded"
    )
    assert (
        context_status((REF,), replace(META, omission_count=None), now=100).health
        == "unknown"
    )
    assert context_status(now=100, blockers=("dependency_changed",)).health == "blocked"
    assert (
        context_status(
            now=100, blockers=("dependency_changed",), access_failed=True
        ).health
        == "unavailable"
    )
    assert (
        context_status((REF,), META, now=100, blockers=("dependency_unknown",)).health
        == "unknown"
    )


def test_safe_projection_excludes_scopes_paths_secrets_and_arbitrary_fields():
    private = "token=seeded-secret /private/repository"
    scope = Scope("owner", RepositoryIdentity("repo", private, "owner/repo"))
    ref = replace(REF, records=(RecordReference("attempt", "id", 1, scope),))
    value = {**asdict(ref), "prompt": private, "transcript": private, "reason": private}
    result = context_status((ref, value), META, now=100).to_json()
    assert private not in result
    assert json.loads(result)["packet_refs"] == [
        {"packet_id": "packet", "sha256": "a" * 64}
    ]
    assert safe_packet_ref({**value, "packet_id": private}) is None
    assert safe_packet_ref({**value, "sha256": private}) is None
    assert (
        context_status((value, {"packet_id": private}), META, now=100).health
        == "unknown"
    )


def test_bounds_and_canonical_serialization():
    refs = tuple(PacketReference(f"p{i:02}", "a" * 64) for i in range(20))
    status = context_status(refs, replace(META, omission_count=10**20), now=100)
    assert status.health == "degraded"
    assert status.to_dict()["omission_count"] == MAX_COUNT
    assert len(status.to_dict()["packet_refs"]) == MAX_PACKET_REFS
    assert (
        status.to_json()
        == context_status(
            tuple(reversed(refs)), replace(META, omission_count=10**20), now=100
        ).to_json()
    )
    for invalid in (-1, True, 1.2, "secret"):
        assert replace(META, omission_count=invalid).to_dict()["omission_count"] is None
    for invalid in (-1, MAX_TIMESTAMP + 1, True, float("nan")):
        assert replace(META, observed_at=invalid).to_dict()["observed_at"] is None
    with pytest.raises(ValueError, match="Invalid dependency blocker"):
        context_status(now=100, blockers=("token=secret /private/path",))


def test_dependency_mapping_ignores_exception_text_and_unverified_success():
    for evidence in (
        ValueError("token=secret /private/path"),
        {"status": "unknown", "reason": "not merged /private/path"},
        {"status": "blocked", "code": "not_merged"},
    ):
        assert dependency_blockers(evidence, verified=True) == ("dependency_unknown",)
    assert dependency_blockers({"status": "satisfied"}) == ("dependency_unknown",)
    assert dependency_blockers({"status": "satisfied"}, verified=True) == ()
    for field, code in (
        ("access_failed", "dependency_unavailable"),
        ("invalidated", "dependency_changed"),
        ("recovery_pending", "dependency_recovery_pending"),
    ):
        assert dependency_blockers({field: True}, verified=True) == (code,)
        assert dependency_blockers({field: "true"}, verified=True) == (
            "dependency_unknown",
        )
