"""Pure allowlisted snapshot projections; never read context archives."""

from collections.abc import Mapping

from ai_agent.context_status import (
    PacketMetadata,
    context_status,
    dependency_blockers,
    freshness,
)
from ai_agent.context_validation import ContextValidationError, opaque_id


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


def task_context_status(task: Mapping, data: Mapping, *, now: int) -> dict:
    """Project one identity-bound receipt; missing observations stay unknown."""
    source = _source(task, data)
    spec = _identity(task, source)
    if not spec:
        return context_status(now=now).to_dict()
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
    ).to_dict()


def initiative_statuses(data: Mapping, *, now: int) -> list[dict]:
    """List known initiative IDs deterministically, without promoting child health.

    Task receipts cover one task/attempt, not the entire initiative. There is
    currently no initiative-wide observation producer, so its status is unknown.
    """
    identities = set()
    for task in data.get("tasks") or []:
        if isinstance(task, Mapping):
            spec = _identity(task, _source(task, data))
            if spec:
                identities.add(spec["initiative_id"])
    return [
        {"id": identity, "context_status": context_status(now=now).to_dict()}
        for identity in sorted(identities)
    ]
