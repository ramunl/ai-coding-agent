"""Fresh predecessor evidence; display caches never authorize execution."""

import asyncio
import hashlib
import json
import re
import subprocess
import time
from copy import deepcopy
from dataclasses import asdict

from ai_agent.config import CHAT_ID
from ai_agent.context_models import AttemptSnapshot
from ai_agent.context_store import ContextStore
from ai_agent.context_validation import decode_dependency, opaque_id
from ai_agent.dependency_checkout import StaleCheckoutError, synchronize

EVIDENCE_AGE = 60
MAX_DEPENDENCIES = 8


def dependency_ids(task: dict) -> list[str]:
    """Decode old saved edges without iterating strings or discarding corruption."""
    values = task.get("dependency_ids", [])
    if not isinstance(values, (list, tuple)) or len(values) > MAX_DEPENDENCIES:
        raise ValueError("Invalid legacy dependencies")
    values = list(values)
    if task.get("after") is not None:
        values.append(task["after"])
    for value in values:
        opaque_id(value)
    return list(dict.fromkeys(values))


def bind_dependencies(task: dict, data: dict) -> list[dict]:
    """Pin available handoffs before task-list eviction, never invent an identity."""
    if task.get("dependency_invalidations"):
        raise ValueError("Predecessor attempt changed; owner must review dependency")
    values = deepcopy(task.get("dependencies", []))
    if not isinstance(values, list) or len(values) > MAX_DEPENDENCIES:
        raise ValueError("Invalid dependencies")
    owner = str(CHAT_ID)
    known = {decode_dependency(item, owner).task_id for item in values}
    for task_id in dependency_ids(task):
        if task_id not in known:
            previous = next(
                (t for t in data.get("tasks", []) if t.get("id") == task_id), {}
            )
            ref = previous.get("context_handoff")
            scope = (ref or {}).get("scope") or (previous.get("dependency_scope"))
            if not scope:
                raise ValueError("Predecessor scope or archived attempt unavailable")
            values.append({"task_id": task_id, "scope": scope})
    for item in values:
        dependency = decode_dependency(item, owner)
        previous = next(
            (t for t in data.get("tasks", []) if t.get("id") == dependency.task_id),
            None,
        )
        if previous is not None:
            ref = previous.get("context_handoff")
            if ref and not item.get("attempt"):
                item["attempt"] = deepcopy(ref)
            if item.get("attempt") and ref != item["attempt"]:
                task.setdefault("dependency_invalidations", {})[dependency.task_id] = (
                    "attempt changed"
                )
                raise ValueError("Predecessor attempt changed; review dependency")
            # Reopen retains old handoffs for history, not readiness.
            if previous.get("stage") in {"todo", "planning", "planned", "implementing"}:
                raise ValueError("Predecessor has an unfinished attempt")
        decode_dependency(item, owner)
    if len(values) > MAX_DEPENDENCIES:
        raise ValueError("Dependency limit exceeded")
    if values:
        task["dependencies"] = values
    return values


def fingerprint(task: dict, data: dict) -> str:
    """Pure identity/revision binding for display caches and async race detection."""
    predecessors = [
        {
            key: t.get(key)
            for key in ("id", "attempt", "stage", "context_handoff", "branch")
        }
        for t in data.get("tasks", [])
    ]
    value = [
        {
            key: task.get(key)
            for key in (
                "after",
                "dependency_ids",
                "dependencies",
                "attempt",
                "dependency_invalidations",
            )
        },
        predecessors,
    ]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def cached_reason(task: dict, data: dict) -> str | None:
    """Display-only readiness; performs no Git, archive, network or Ops reads."""
    if data.get("context_recovery_pending"):
        return "context handoff recovery"
    try:
        if task.get("dependency_invalidations"):
            return "predecessor attempt changed; review dependency"
        ids = dependency_ids(task)
        dependencies = task.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise ValueError("Invalid dependencies")
        if not ids and not dependencies:
            return None
        for item in dependencies:
            decode_dependency(item, str(CHAT_ID))
        result = task.get("dependency_check") or {}
        if (
            result.get("binding") != fingerprint(task, data)
            or not 0 <= time.time() - result.get("checked_at", 0) <= EVIDENCE_AGE
        ):
            return "dependency evidence unknown; fresh verification required"
        return (
            None
            if result.get("status") == "satisfied"
            else result.get("reason", "dependency evidence unknown")
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return "dependency evidence unknown; invalid dependency record"


def git(repo: str, *args: str) -> str:
    """Read the captured checkout only, with bounded commands and no fetch."""
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("Checkout evidence unavailable")
    return result.stdout.strip()


def ancestor(repo: str, commit: str, head: str) -> None:
    """Squash merges use GitHub's merge commit; missing/shallow history blocks."""
    if not all(
        isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value)
        for value in (commit, head)
    ):
        raise ValueError("Commit evidence unavailable")
    git(repo, "cat-file", "-e", commit + "^{commit}")
    git(repo, "merge-base", "--is-ancestor", commit, head)


def evaluate_dependency(value: dict, *, store: ContextStore | None = None) -> dict:
    """Re-read the pinned archive and PR, then verify the prerequisite checkout."""
    from ai_agent.task_pulls import verified_pull

    dependency = decode_dependency(value, str(CHAT_ID))
    if dependency.override is not None:
        from ai_agent.dependency_overrides import verify_override

        return verify_override(dependency, store)
    ref = dependency.attempt
    if ref is None:
        raise ValueError("Predecessor attempt evidence unavailable")
    store = store or ContextStore(dependency.scope)
    if store.scope != dependency.scope:
        raise ValueError("Archive scope mismatch")
    record = store.get("attempt", ref.record_id, ref.revision)
    if (
        not isinstance(record, AttemptSnapshot)
        or record.task_id != dependency.task_id
        or record.attempt_id != ref.record_id
    ):
        raise ValueError("Predecessor identity mismatch")
    urls = {e.locator for e in record.evidence if e.source == "pull_request"}
    commits = {
        e.locator
        for e in record.evidence
        if e.source == "commit" and re.fullmatch(r"[a-f0-9]{40}", e.locator)
    }
    if len(urls) != 1 or not commits:
        raise ValueError("Incomplete or conflicting attempt PR evidence")
    repository = dependency.scope.repository
    pull = verified_pull(
        repository.github_repository,
        urls.pop(),
        next(iter(commits)) if len(commits) == 1 else commits,
    )
    if not 0 <= time.time() - pull["checked_at"] <= EVIDENCE_AGE:
        raise ValueError("PR evidence stale")
    facts = {"dependency": asdict(dependency), "pull": pull}
    if dependency.policy == "reviewable_pr":
        if not pull["reviewable"]:
            raise ValueError("PR is not open and non-draft")
        return facts
    if not pull["merged"]:
        raise ValueError("Prerequisite PR is not merged")
    path = repository.repo_path
    remote = git(path, "remote", "get-url", "origin")
    if remote.removesuffix(".git") not in {
        f"https://github.com/{repository.github_repository}",
        f"git@github.com:{repository.github_repository}",
    }:
        raise ValueError("Checkout repository identity mismatch")
    head = git(path, "rev-parse", "HEAD")
    try:
        ancestor(path, pull["merge_commit"], head)
    except ValueError as error:
        raise StaleCheckoutError(repository, pull["merge_commit"]) from error
    facts["checkout_head"] = head
    if dependency.policy == "deployed":
        from ai_agent.deployment_evidence import deployment_evidence

        deployed = deployment_evidence(repository)
        ancestor(path, pull["merge_commit"], deployed["commit"])
        facts["deployment"] = deployed
    if git(path, "rev-parse", "HEAD") != head:
        raise ValueError("Checkout changed during dependency verification")
    return facts


def _evaluate(values: list[dict]) -> list[dict]:
    facts = [evaluate_dependency(value) for value in values]
    for fact in facts:
        if (
            "pull" in fact
            and not 0 <= time.time() - fact["pull"]["checked_at"] <= EVIDENCE_AGE
        ):
            raise ValueError("PR evidence expired during verification")
        if "checkout_head" in fact:
            path = fact["dependency"]["scope"]["repository"]["repo_path"]
            if git(path, "rev-parse", "HEAD") != fact["checkout_head"]:
                raise ValueError("Checkout changed during verification")
    return facts


async def refresh(task: dict, data: dict) -> str | None:
    """Fresh admission check off the event loop; never reuse a saved decision."""
    task.pop("dependency_check", None)
    try:
        values = bind_dependencies(task, data)
        binding = fingerprint(task, data)
        # A merged prerequisite may be newer than the planner's checkout.
        # Retry all evidence after each safe fast-forward, bounded per edge.
        for attempt in range(len(values) + 1):
            try:
                facts = await asyncio.to_thread(_evaluate, values) if values else []
                break
            except StaleCheckoutError as stale:
                if attempt == len(values):
                    raise ValueError(
                        "Planner checkout still lacks a merged prerequisite"
                    ) from stale
                await synchronize(stale, data)
        if fingerprint(task, data) != binding:
            raise ValueError("Dependencies changed during verification")
        result = {
            "status": "satisfied",
            "facts": facts,
            "reason": "owner-reviewed dependency override"
            if any("override" in f for f in facts)
            else "prerequisites verified",
        }
    except Exception as error:
        # No raw source payloads or network errors enter display/prompt text.
        result = {
            "status": "unknown",
            "reason": "dependency evidence unknown: "
            + (str(error) if type(error) is ValueError else type(error).__name__),
        }
        binding = fingerprint(task, data)
    task["dependency_check"] = {**result, "binding": binding, "checked_at": time.time()}
    return cached_reason(task, data)


async def review_replacements(task: dict, data: dict) -> None:
    """An explicit owner start can review replacement handoffs before planning."""
    if task.get("stage") != "todo" or task.get("run") or task.get("plan_id"):
        return
    candidate = deepcopy(task)
    changed = []
    for edge in candidate.get("dependencies", []):
        dependency = decode_dependency(edge, str(CHAT_ID))
        previous = next(
            (t for t in data.get("tasks", []) if t.get("id") == dependency.task_id), {}
        )
        ref = previous.get("context_handoff")
        if ref == edge.get("attempt"):
            continue
        if (
            not ref
            or ref.get("scope") != edge.get("scope")
            or dependency.override is not None
            or previous.get("stage") not in {"done", "pr"}
        ):
            return
        changed.append(
            {
                "task_id": dependency.task_id,
                "before": edge.get("attempt"),
                "after": deepcopy(ref),
            }
        )
        edge["attempt"] = deepcopy(ref)
        edge["revision"] = edge.get("revision", 1) + 1
    if not changed:
        return
    binding = fingerprint(task, data)
    candidate.pop("dependency_invalidations", None)
    if await refresh(candidate, data) is not None:
        return
    if fingerprint(task, data) != binding:
        raise ValueError("Dependencies changed during owner review")
    task["dependencies"] = candidate["dependencies"]
    task.pop("dependency_invalidations", None)
    task.pop("dependency_check", None)
    task.setdefault("dependency_reviews", []).append(
        {
            "at": time.time(),
            "owner_id": str(CHAT_ID),
            "source": "start_task",
            "changes": changed,
        }
    )


def invalidate_dependents(data: dict, predecessor_id: str) -> None:
    """Retain reopen invalidation on consumers and queued snapshots after eviction."""
    consumers = list(data.get("tasks", []))
    for item in [
        *(data.get("task_queue") or []),
        data.get("pending_implementation") or {},
    ]:
        if isinstance(item.get("dependency_task"), dict):
            consumers.append(item["dependency_task"])
    for task in consumers:
        try:
            ids = dependency_ids(task) + [
                d["task_id"] for d in task.get("dependencies", [])
            ]
            if predecessor_id in ids:
                task.setdefault("dependency_invalidations", {})[predecessor_id] = (
                    "attempt reopened"
                )
                task.pop("dependency_check", None)
                saved = (task.get("run") or {}).get("dependency_task")
                if saved is not None:
                    saved["dependency_invalidations"] = deepcopy(
                        task["dependency_invalidations"]
                    )
        except (ValueError, TypeError, KeyError):
            # Already malformed dependencies remain blocked by normal decoding.
            continue
