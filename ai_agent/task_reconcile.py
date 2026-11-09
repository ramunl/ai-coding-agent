"""Owner-requested recovery of identity-bound merged task attempts."""

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy

from ai_agent.context_models import AttemptSnapshot
from ai_agent.context_store import ContextStore
from ai_agent.context_validation import decode_dependency
from ai_agent.dependency_evidence import CHAT_ID
from ai_agent.projects import get_project
from ai_agent.task_pulls import verified_pull
from ai_agent.tasks import set_stage


def _candidate(task: dict, data: dict) -> dict:
    """Refuse execution or replacement work before considering an old completion."""
    if data.get("context_recovery_pending"):
        raise ValueError("Context recovery is pending; resolve it first")
    if task.get("stage") not in {"todo", "stopped"}:
        raise ValueError("Only an idle To do or Stopped task can be reconciled")
    if data.get("planning_task") == task["id"]:
        raise ValueError("This task is being planned")
    pending = data.get("pending_plan")
    if task.get("plan_id") and task["plan_id"] == getattr(pending, "id", None):
        raise ValueError("This task still has a pending plan; inspect it first")
    current = (data.get("worker") or {}).get("current") or {}
    if current.get("task") == task["id"]:
        raise ValueError("The worker is still finishing this task")
    runs = [
        *(data.get("task_queue") or []),
        data.get("active_execution") or {},
        data.get("pending_implementation") or {},
    ]
    history = task.get("attempt_history") or []
    branches = {task.get("branch"), *(item.get("branch") for item in history)} - {None}
    if any(
        run.get("checkpoint_task") == task["id"]
        or run.get("task_id") == task["id"]
        or run.get("branch", run.get("branch_name")) in branches
        for run in runs
    ):
        raise ValueError("This task has active or queued work")
    candidate = deepcopy(task)
    if task.get("stage") == "todo":
        if (
            task.get("branch")
            or task.get("run")
            or task.get("plan_id")
            or task.get("context_handoff")
        ):
            raise ValueError(
                "The reopened attempt already has new work; inspect it first"
            )
        refs = task.get("context_handoffs") or []
        if not refs or not history:
            raise ValueError("No archived attempt is available")
        candidate["context_handoff"] = deepcopy(refs[-1])
        candidate["branch"] = history[-1].get("branch")
    if not candidate.get("context_handoff"):
        raise ValueError("No identity-bound attempt is available")
    return candidate


def _completion(task: dict) -> tuple[dict, dict]:
    """Verify a concrete archived PR and head, never a branch-only search."""
    ref = task["context_handoff"]
    dep = decode_dependency(
        {"task_id": task["id"], "scope": ref["scope"], "attempt": ref}, str(CHAT_ID)
    )
    project = get_project(task["repo"])
    repository = dep.scope.repository
    if repository is None or (
        repository.project_name != task["repo"]
        or repository.github_repository != project.github_repository
        or repository.repo_path != str(project.repo_path)
    ):
        raise ValueError("Archived attempt repository mismatch")
    record = ContextStore(dep.scope).get(
        "attempt", dep.attempt.record_id, dep.attempt.revision
    )
    if (
        not isinstance(record, AttemptSnapshot)
        or record.task_id != task["id"]
        or record.attempt_id != dep.attempt.record_id
    ):
        raise ValueError("Archived attempt task identity mismatch")
    heads = {e.locator for e in record.evidence if e.source == "commit"}
    urls = {e.locator for e in record.evidence if e.source == "pull_request"}
    if len(urls) != 1 or not heads:
        raise ValueError("Missing or ambiguous archived PR evidence")
    facts = verified_pull(repository.github_repository, next(iter(urls)), heads)
    if not facts["merged"] or not facts.get("merge_commit"):
        raise ValueError("The verified PR is not merged; no task state was changed")
    return facts, deepcopy(ref)


def _binding(task: dict, data: dict) -> str:
    """Detect edits to this task or its consumers while GitHub is being checked."""
    relevant = [task]
    for consumer in data.get("tasks", []):
        edges = consumer.get("dependencies") or []
        if consumer.get("after") == task["id"] or any(
            isinstance(edge, dict) and edge.get("task_id") == task["id"]
            for edge in edges
        ):
            relevant.append(consumer)
    return json.dumps(
        [
            {
                k: v
                for k, v in item.items()
                if k not in {"dependency_check", "dependency_observation"}
            }
            for item in relevant
        ],
        sort_keys=True,
    )


def _restore_edges(task: dict, data: dict, ref: dict) -> list[str]:
    """Review only unchanged, unstarted consumers already pinned to this attempt."""
    reviewed = []
    pending = data.get("pending_plan")
    for consumer in data.get("tasks", []):
        if consumer.get("stage") not in {"todo", "planned"} or consumer.get("run"):
            continue
        if data.get("planning_task") == consumer.get("id"):
            continue
        if consumer.get("plan_id") == getattr(pending, "id", None) and getattr(
            pending, "approved", False
        ):
            continue
        values = consumer.get("dependencies", [])
        if not isinstance(values, list) or not isinstance(
            consumer.get("dependency_reviews", []), list
        ):
            continue
        edges = [
            edge
            for edge in values
            if isinstance(edge, dict) and edge.get("task_id") == task["id"]
        ]
        invalidations = consumer.get("dependency_invalidations") or {}
        if not isinstance(invalidations, dict):
            continue
        if not edges or any(
            edge.get("attempt") != ref
            or edge.get("scope") != ref["scope"]
            or edge.get("override")
            for edge in edges
        ):
            continue
        if invalidations.get(task["id"]) not in {"attempt reopened", "attempt changed"}:
            continue
        invalidations.pop(task["id"])
        if not invalidations:
            consumer.pop("dependency_invalidations", None)
        consumer.pop("dependency_check", None)
        consumer.setdefault("dependency_reviews", []).append(
            {
                "at": time.time(),
                "owner_id": str(CHAT_ID),
                "source": "reconcile_task",
                "predecessor": task["id"],
                "attempt": deepcopy(ref),
            }
        )
        reviewed.append(consumer["id"])
    return reviewed


async def reconcile(task: dict, data: dict) -> str:
    """Restore a verified completion; leave normal fresh dependency admission intact."""
    candidate = _candidate(task, data)
    binding = _binding(task, data)
    facts, ref = await asyncio.to_thread(_completion, candidate)
    if binding != _binding(task, data) or not any(
        t is task for t in data.get("tasks", [])
    ):
        raise ValueError(
            "Task or dependencies changed during verification; check again"
        )
    _candidate(task, data)
    task.setdefault("completion_reviews", []).append(
        {
            "at": time.time(),
            "owner_id": str(CHAT_ID),
            "previous_stage": task["stage"],
            "attempt": deepcopy(ref),
            "pr_url": facts["url"],
            "head": facts["head"],
        }
    )
    task.update(
        context_handoff=ref,
        branch=candidate.get("branch"),
        pr_url=facts["url"],
        merged_at=facts["merged_at"],
        merge_commit=facts["merge_commit"],
        autopilot=False,
    )
    task.pop("start_next", None)
    task.pop("dependency_check", None)
    set_stage(task, "done")
    reviewed = _restore_edges(task, data, ref)
    return (
        f"Confirmed merged: {facts['url']}. Task restored to Done. "
        f"Reviewed {len(reviewed)} unchanged dependent link(s); "
        "fresh prerequisite verification is still required before execution."
    )
