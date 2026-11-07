"""Pure allowlisted snapshot projections; never read context archives."""

from collections.abc import Mapping

from ai_agent.context_status import (
    ContextStatus,
    PacketMetadata,
    context_status,
    dependency_blockers,
    freshness,
)
from ai_agent.context_validation import ContextValidationError, opaque_id

# A task with no context identity is "not planned" only while it can still be
# planned; finished legacy work without an identity stays unknown.
NOT_PLANNED_STAGES = frozenset({"proposed", "todo", "planning"})
_EVIDENCE_ORDER = (
    "unknown",
    "not_planned",
    "metadata_unavailable",
    "stale",
    "verified_current",
)


def _identifier(value: object) -> str | None:
    try:
        return opaque_id(value)
    except ContextValidationError:
        return None


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _source(task: Mapping, data: Mapping) -> Mapping:
    """Use the current matching plan, otherwise the task's retained run/receipt."""
    plan = data.get("pending_plan")
    if task.get("plan_id") is not None and getattr(plan, "id", None) == task["plan_id"]:
        return {
            "context_spec": getattr(plan, "context_spec", None),
            "context_packet": getattr(plan, "context_packet", None),
            "packet_metadata": getattr(plan, "packet_metadata", None),
        }
    return _mapping(task.get("run")) if task.get("run") is not None else task


def _identity(task: Mapping, source: Mapping) -> Mapping:
    spec = _mapping(source.get("context_spec"))
    task_id = _identifier(task.get("id"))
    if (
        task_id is None
        or spec.get("task_id") != task_id
        or _identifier(spec.get("initiative_id")) is None
        or _identifier(spec.get("attempt_id")) is None
    ):
        return {}
    return spec


def _receipt_status(task: Mapping, source: Mapping, spec: Mapping, *, now: int) -> ContextStatus:
    """Project one identity-bound receipt into the typed v1 status."""
    metadata = PacketMetadata.from_dict(source.get("packet_metadata"))
    packet = source.get("context_packet", source.get("packet"))
    blockers = ()
    if any(
        container.get(key)
        for container in (task, spec)
        for key in ("after", "dependencies", "dependency_ids")
    ):
        # Existing free-text dependency caches cannot establish finer codes.
        # A structured observation must bind to this exact context identity.
        evidence = _mapping(source.get("dependency_observation"))
        observation = PacketMetadata.from_dict(evidence.get("metadata"))
        verified = (
            evidence.get("context_spec") == spec
            and freshness(observation, now=now) == "fresh"
        )
        blockers = dependency_blockers(dict(evidence), verified=verified)
    return context_status(
        (packet,) if packet is not None else (),
        metadata,
        now=now,
        blockers=blockers,
        planned=True,
    )


def _no_receipt_status(task: Mapping, source: Mapping, *, now: int) -> ContextStatus:
    """A subject without a receipt: not planned yet, or legacy/unknown."""
    if _mapping(source).get("context_spec"):
        return context_status(now=now)
    planned = False if task.get("stage") in NOT_PLANNED_STAGES else None
    return context_status(now=now, planned=planned)


def task_context_status(task: Mapping, data: Mapping, *, now: int) -> dict:
    """Project one identity-bound receipt; missing observations stay unknown."""
    source = _source(task, data)
    spec = _identity(task, source)
    if not spec:
        return _no_receipt_status(task, source, now=now).to_dict()
    return _receipt_status(task, source, spec, now=now).to_dict()


def _sum_or_none(values: list[int | None]) -> int | None:
    """Conservative aggregation: one unknown count keeps the total unknown."""
    if any(value is None for value in values):
        return None
    return sum(values)


def _worst_evidence(states: list[str]) -> str:
    """The most conservative evidence state wins; never promote child health."""
    return min(states, key=_EVIDENCE_ORDER.index)


def _initiative_status(entries: list[dict], *, now: int) -> ContextStatus:
    """Aggregate retained receipts into conservative initiative coverage.

    Health stays unknown unless every retained receipt is verified-current;
    otherwise the most conservative child evidence state is reported.
    """
    statuses = [
        _receipt_status(entry["task"], entry["source"], entry["spec"], now=now)
        for entry in entries
    ]
    if not all(status.evidence_state == "verified_current" for status in statuses):
        return ContextStatus(
            "unknown",
            (),
            "unknown",
            None,
            None,
            None,
            (),
            _worst_evidence([status.evidence_state for status in statuses]),
        )
    refs = tuple(
        sorted({ref for status in statuses for ref in status.packet_refs})
    )
    metadata = PacketMetadata(
        min(status.observed_at for status in statuses if status.observed_at is not None),
        True,
        _sum_or_none([status.omission_count for status in statuses]),
        _sum_or_none([status.truncation_count for status in statuses]),
    )
    blockers = tuple(
        sorted(
            {code for status in statuses for code in status.dependency_blockers}
        )
    )
    return context_status(
        tuple(ref.to_dict() for ref in refs),
        metadata,
        now=now,
        blockers=blockers,
        planned=True,
    )


def initiative_statuses(data: Mapping, *, now: int) -> list[dict]:
    """List known initiative IDs deterministically with conservative coverage.

    Retained receipts cover one task/attempt each. An initiative is healthy
    only when every retained receipt is verified-current; any stale, missing
    or unknown receipt keeps coverage unknown with the worst child state.
    """
    receipts: dict[str, list[dict]] = {}
    for task in data.get("tasks") or []:
        if not isinstance(task, Mapping):
            continue
        source = _source(task, data)
        spec = _identity(task, source)
        if not spec:
            continue
        receipts.setdefault(spec["initiative_id"], []).append(
            {"task": task, "source": source, "spec": spec}
        )
    return [
        {"id": identity, "context_status": _initiative_status(entries, now=now).to_dict()}
        for identity, entries in sorted(receipts.items())
    ]
