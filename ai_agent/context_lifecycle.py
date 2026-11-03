"""Approval bindings and explicit, provider-neutral lifecycle context.

Legacy work has no context_spec and remains archive-free. A context spec is
trusted application state, never parsed from a model response or prompt.
"""

import hashlib
import json
from contextvars import ContextVar
from dataclasses import asdict
from uuid import uuid4

from ai_agent.context_models import RecordReference, RepositoryIdentity, Scope
from ai_agent.context_packets import build_packet
from ai_agent.context_sources import ContextBlockedError, ContextRequest
from ai_agent.context_store import ContextStore

packet_receipt: ContextVar[dict | None] = ContextVar(
    "planning_packet_receipt", default=None
)

current_context: ContextVar[ContextRequest | None] = ContextVar(
    "lifecycle_context", default=None
)


def digest(value: object) -> str:
    """Hash exact canonical data, without retaining execution payloads."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def new_spec(owner: str, identity: dict, task_id: str | None = None) -> dict:
    """Capture scope at an authorized entry point, before any await."""
    task_id = task_id or uuid4().hex
    return {
        "scope": asdict(Scope(owner, RepositoryIdentity(**identity))),
        "initiative_id": task_id,
        "task_id": task_id,
        "attempt_id": uuid4().hex,
    }


def request_from_spec(spec: dict) -> ContextRequest:
    """Restore only declared scopes and pinned references, never active scope."""

    def scope(value: dict) -> Scope:
        repository = value.get("repository")
        return Scope(
            value["owner_id"], RepositoryIdentity(**repository) if repository else None
        )

    def references(name: str) -> tuple:
        return tuple(
            RecordReference(
                item["kind"], item["record_id"], item["revision"], scope(item["scope"])
            )
            for item in spec.get(name, [])
        )

    from ai_agent.context_validation import decode_dependency
    from ai_agent.dependency_evidence import dependency_ids

    dependencies = spec.get("dependencies", [])
    if not isinstance(dependencies, (list, tuple)) or len(dependencies) > 8:
        raise ContextBlockedError("Invalid scoped dependencies")
    captured = scope(spec["scope"])
    constraints = references("constraints")
    prerequisites = references("prerequisites")
    candidates = references("candidates")
    scopes = {captured, *(r.scope for r in constraints + prerequisites + candidates)}
    request = ContextRequest(
        scope=captured,
        authorized_owner_id=captured.owner_id,
        initiative_id=spec["initiative_id"],
        task_id=spec["task_id"],
        attempt_id=spec["attempt_id"],
        stores=tuple(ContextStore(value) for value in scopes),
        constraints=constraints,
        prerequisites=prerequisites,
        candidates=candidates,
        dependency_ids=tuple(dependency_ids(spec)),
        dependencies=tuple(
            decode_dependency(d, captured.owner_id) for d in dependencies
        ),
        reopen_history=tuple(spec.get("reopen_history", [])),
    )
    request.validate()
    return request


def plan_binding(plan: object) -> dict:
    """Bind the exact plan, repository, images and authoritative packet revisions."""
    spec = plan.context_spec
    packet = (
        build_packet(request_from_spec(spec), verify_dependencies=False)
        if spec
        else None
    )
    return json.loads(
        json.dumps(
            {
                "plan": digest([plan.plan_text, plan.repository_identity, plan.images]),
                "revision": plan.revision,
                "packet": plan.context_packet
                or (asdict(packet.reference) if packet else None),
                "sources": asdict(packet.reference) if packet else None,
            }
        )
    )


def approve_plan(plan: object) -> None:
    """Only trusted approval handlers call this after owner authorization."""
    plan.approval_binding = plan_binding(plan)
    plan.approved = True
    plan.review_required = False


def require_plan_approval(plan: object) -> None:
    """Reject material changes; old boolean approvals remain legacy-compatible."""
    try:
        if not plan.approved or (
            plan.approval_binding is not None
            and plan.approval_binding != plan_binding(plan)
        ):
            raise ContextBlockedError("Plan changed; review and approve it again")
        if plan.context_spec and plan.approval_binding is None:
            raise ContextBlockedError("Context plan needs approval")
    except Exception:
        plan.approved = False
        plan.review_required = True
        raise


def run_binding(item: dict) -> str:
    """Seal the material run fields; provider choice is deliberately excluded."""
    return digest(
        {
            key: item.get(key)
            for key in (
                "repository_identity",
                "change",
                "commit_type",
                "pr_body_label",
                "codex_prompt",
                "branch_name",
                "images",
                "context_spec",
                "approval_binding",
            )
        }
    )


def require_run(item: dict) -> None:
    """Shared guard for queued, retried and automatically dispatched work."""
    if item.get("run_binding") and len(str(item.get("codex_prompt") or "")) > 256_000:
        raise ContextBlockedError("Approved prompt exceeds storage limit; narrow plan")
    if item.get("run_binding") and item["run_binding"] != run_binding(item):
        raise ContextBlockedError("Approved work changed; review a fresh plan")
    if item.get("context_spec"):
        packet = build_packet(
            request_from_spec(item["context_spec"]), verify_dependencies=False
        )
        approved = (item.get("approval_binding") or {}).get("sources")
        if approved and digest(approved) != digest(asdict(packet.reference)):
            raise ContextBlockedError("Approved context changed; owner review required")


def recovery_refusal(data: dict) -> str | None:
    """Uncertain effects must be reconciled before any dependent work."""
    if data.get("context_recovery_pending"):
        return "context handoff recovery is pending; reconcile the previous attempt"
    return None


def spec_from_request(request: ContextRequest | None) -> dict | None:
    """Persist context identities without serializing store objects or payloads."""
    if request is None:
        return None
    return {
        "scope": asdict(request.scope),
        "initiative_id": request.initiative_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "constraints": [asdict(ref) for ref in request.constraints],
        "prerequisites": [asdict(ref) for ref in request.prerequisites],
        "candidates": [asdict(ref) for ref in request.candidates],
        "dependency_ids": list(request.dependency_ids),
        "dependencies": [asdict(d) for d in request.dependencies],
        "reopen_history": list(request.reopen_history),
    }


def planning_request(data: dict, owner: str, identity: dict) -> ContextRequest:
    """Compose explicitly pinned sources and task handoffs at the owner boundary."""
    from ai_agent.task_reopen import attempt_history

    task = next(
        (t for t in data.get("tasks", []) if t.get("id") == data.get("planning_task")),
        {},
    )
    spec = new_spec(owner, identity, task.get("id"))
    sources = data.get("context_sources") or {}
    for key in (
        "constraints",
        "prerequisites",
        "candidates",
        "dependency_ids",
        "dependencies",
    ):
        if key in sources:
            spec[key] = sources[key]
    spec["reopen_history"] = attempt_history(task)
    spec["candidates"] = [
        *spec.get("candidates", []),
        *task.get("context_handoffs", []),
    ]
    from ai_agent.dependency_evidence import bind_dependencies

    try:
        if not isinstance(spec.get("dependencies", []), (list, tuple)):
            raise ValueError("Invalid scoped dependencies")
        spec["dependencies"] = [
            *spec.get("dependencies", []),
            *bind_dependencies(task, data),
        ]
    except (ValueError, TypeError, KeyError) as error:
        raise ContextBlockedError(
            "Dependency evidence unknown; review predecessor"
        ) from error
    return request_from_spec(spec)
