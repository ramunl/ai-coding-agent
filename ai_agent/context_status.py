"""Bounded, display-only context status v1; never an execution authorization."""

import json
import re
from dataclasses import dataclass
from typing import Literal

from ai_agent.context_models import PacketReference

MAX_COUNT = 65_535
MAX_PACKET_REFS = 8
MAX_TIMESTAMP = 253_402_300_799
FRESHNESS_SECONDS = 60
Health = Literal["healthy", "degraded", "blocked", "unknown", "unavailable"]
Freshness = Literal["fresh", "stale", "unknown"]
BlockerCode = Literal[
    "dependency_unknown",
    "dependency_unavailable",
    "dependency_changed",
    "dependency_recovery_pending",
]
BLOCKER_CODES = frozenset(
    {
        "dependency_unknown",
        "dependency_unavailable",
        "dependency_changed",
        "dependency_recovery_pending",
    }
)
EvidenceState = Literal[
    "not_planned",
    "metadata_unavailable",
    "stale",
    "verified_current",
    "unknown",
]
EVIDENCE_STATES = frozenset(
    {
        "not_planned",
        "metadata_unavailable",
        "stale",
        "verified_current",
        "unknown",
    }
)
HEALTH_VALUES = frozenset({"healthy", "degraded", "blocked", "unknown", "unavailable"})
FRESHNESS_VALUES = frozenset({"fresh", "stale", "unknown"})


def _integer(value: object, maximum: int) -> int | None:
    return value if type(value) is int and 0 <= value <= maximum else None


def bounded_count(value: object) -> int | None:
    """Saturate observed counts; invalid or missing values remain unknown."""
    return min(value, MAX_COUNT) if type(value) is int and value >= 0 else None


@dataclass(frozen=True)
class PacketMetadata:
    """Observation outside packet bytes and approval identity; legacy is unknown."""

    observed_at: int | None = None
    verified: bool = False
    omission_count: int | None = None
    truncation_count: int | None = None
    byte_size: int | None = None
    build_latency_ms: int | None = None

    def to_dict(self) -> dict:
        """Project only bounded v1 capture fields, including explicit nulls."""
        return {
            "schema_version": 1,
            "observed_at": _integer(self.observed_at, MAX_TIMESTAMP),
            "verified": self.verified is True,
            "omission_count": bounded_count(self.omission_count),
            "truncation_count": bounded_count(self.truncation_count),
            "byte_size": bounded_count(self.byte_size),
            "build_latency_ms": bounded_count(self.build_latency_ms),
        }

    @classmethod
    def from_dict(cls, value: object) -> "PacketMetadata":
        """Read additive receipts without inferring observations for old packets."""
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
            return cls()
        if value["schema_version"] != 1:
            return cls()
        return cls(
            _integer(value.get("observed_at"), MAX_TIMESTAMP),
            value.get("verified") is True,
            bounded_count(value.get("omission_count")),
            bounded_count(value.get("truncation_count")),
            bounded_count(value.get("byte_size")),
            bounded_count(value.get("build_latency_ms")),
        )


@dataclass(frozen=True, order=True)
class SafePacketReference:
    """Only validated packet identity and content digest may leave the archive."""

    packet_id: str
    sha256: str

    def to_dict(self) -> dict:
        """Reject invalid identities without including input in errors."""
        if not (
            isinstance(self.packet_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.packet_id)
            and isinstance(self.sha256, str)
            and re.fullmatch(r"[a-f0-9]{64}", self.sha256)
        ):
            raise ValueError("Invalid safe packet reference")
        return {"packet_id": self.packet_id, "sha256": self.sha256}


def safe_packet_ref(value: object) -> SafePacketReference | None:
    """Never serialize scoped record references or arbitrary metadata."""
    if isinstance(value, PacketReference):
        ref = SafePacketReference(value.packet_id, value.sha256)
    elif isinstance(value, dict):
        ref = SafePacketReference(value.get("packet_id"), value.get("sha256"))
    else:
        return None
    try:
        ref.to_dict()
    except ValueError:
        return None
    return ref


def freshness(metadata: PacketMetadata, *, now: int) -> Freshness:
    """A future, missing or unverified observation is unknown, never fresh."""
    observed = _integer(metadata.observed_at, MAX_TIMESTAMP)
    if (
        metadata.verified is not True
        or observed is None
        or _integer(now, MAX_TIMESTAMP) is None
        or now < observed
    ):
        return "unknown"
    return "fresh" if now - observed <= FRESHNESS_SECONDS else "stale"


def dependency_blockers(
    evidence: object, *, verified: bool = False
) -> tuple[BlockerCode, ...]:
    """Map trusted structured facts only; ignore reasons, exceptions and payloads.

    The caller must verify freshness and identity before passing verified=True.
    Current free-text dependency failures deliberately map to unknown.
    """
    if not isinstance(evidence, dict) or verified is not True:
        return ("dependency_unknown",)
    codes: list[BlockerCode] = []
    for field, code in (
        ("access_failed", "dependency_unavailable"),
        ("invalidated", "dependency_changed"),
        ("recovery_pending", "dependency_recovery_pending"),
    ):
        if evidence.get(field) is True:
            codes.append(code)
    if codes:
        return tuple(sorted(codes))
    return () if evidence.get("status") == "satisfied" else ("dependency_unknown",)


@dataclass(frozen=True)
class ContextStatus:
    """Typed v1 output; construct via context_status for derived health."""

    health: Health
    packet_refs: tuple[SafePacketReference, ...]
    freshness: Freshness
    observed_at: int | None
    omission_count: int | None
    truncation_count: int | None
    dependency_blockers: tuple[BlockerCode, ...]
    evidence_state: EvidenceState = "unknown"

    def to_dict(self) -> dict:
        """Validate closed enums and explicitly project deterministic JSON fields."""
        if self.health not in HEALTH_VALUES:
            raise ValueError("Invalid context health")
        if self.freshness not in FRESHNESS_VALUES:
            raise ValueError("Invalid context freshness")
        if self.evidence_state not in EVIDENCE_STATES:
            raise ValueError("Invalid evidence state")
        codes = tuple(sorted(set(self.dependency_blockers)))
        if not set(codes) <= BLOCKER_CODES:
            raise ValueError("Invalid dependency blocker code")
        refs = sorted(set(self.packet_refs))[:MAX_PACKET_REFS]
        return {
            "schema_version": 1,
            "health": self.health,
            "packet_refs": [ref.to_dict() for ref in refs],
            "freshness": self.freshness,
            "observed_at": _integer(self.observed_at, MAX_TIMESTAMP),
            "omission_count": bounded_count(self.omission_count),
            "truncation_count": bounded_count(self.truncation_count),
            "dependency_blockers": list(codes),
            "evidence_state": self.evidence_state,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ContextStatus":
        """Restore a projected status; a missing additive field stays unknown.

        Legacy records predate `evidence_state`, so an absent or invalid value
        defaults to "unknown" without raising or migrating.
        """
        if not isinstance(value, dict):
            return cls("unknown", (), "unknown", None, None, None, ())
        health = value.get("health") if value.get("health") in HEALTH_VALUES else "unknown"
        freshness = (
            value.get("freshness")
            if value.get("freshness") in FRESHNESS_VALUES
            else "unknown"
        )
        evidence = value.get("evidence_state")
        evidence_state = evidence if evidence in EVIDENCE_STATES else "unknown"
        refs = []
        for item in value.get("packet_refs") or []:
            ref = safe_packet_ref(item)
            if ref is not None:
                refs.append(ref)
        codes = tuple(
            sorted(
                {
                    code
                    for code in value.get("dependency_blockers") or []
                    if code in BLOCKER_CODES
                }
            )
        )
        return cls(
            health,
            tuple(refs)[:MAX_PACKET_REFS],
            freshness,
            _integer(value.get("observed_at"), MAX_TIMESTAMP),
            bounded_count(value.get("omission_count")),
            bounded_count(value.get("truncation_count")),
            codes,
            evidence_state,
        )

    def to_json(self) -> str:
        """Serialize canonical ASCII JSON without clocks or external reads."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def context_status(
    packet_refs: tuple[object, ...] = (),
    metadata: PacketMetadata | None = None,
    *,
    now: int,
    access_failed: bool = False,
    blockers: tuple[BlockerCode, ...] = (),
    planned: bool | None = None,
) -> ContextStatus:
    """Derive display health conservatively from explicit observations only.

    `planned` describes whether the subject carries a context identity at all:
    True for a receipt, False for a subject known not to be planned yet, and
    None for legacy/unknown records. It selects the additive `evidence_state`
    and never changes health or freshness.
    """
    metadata = PacketMetadata.from_dict((metadata or PacketMetadata()).to_dict())
    refs = tuple(sorted({r for v in packet_refs if (r := safe_packet_ref(v))}))
    state = freshness(metadata, now=now)
    counts = (metadata.omission_count, metadata.truncation_count)
    codes = tuple(sorted(set(blockers)))
    if not set(codes) <= BLOCKER_CODES:
        raise ValueError("Invalid dependency blocker code")
    if state == "fresh":
        evidence_state = "verified_current"
    elif state == "stale":
        evidence_state = "stale"
    elif planned is True:
        evidence_state = "metadata_unavailable"
    elif planned is False:
        evidence_state = "not_planned"
    else:
        evidence_state = "unknown"
    if access_failed is True or "dependency_unavailable" in codes:
        health = "unavailable"
    elif set(codes) - {"dependency_unknown"}:
        health = "blocked"
    elif (
        state == "unknown"
        or None in counts
        or not refs
        or "dependency_unknown" in codes
        or any(safe_packet_ref(value) is None for value in packet_refs)
    ):
        health = "unknown"
    elif state == "stale" or any(counts) or len(refs) > MAX_PACKET_REFS:
        health = "degraded"
    else:
        health = "healthy"
    return ContextStatus(
        health,
        refs[:MAX_PACKET_REFS],
        state,
        metadata.observed_at,
        *counts,
        codes,
        evidence_state,
    )
