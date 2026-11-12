"""Owner-only resolution of one identity-bound attempt stuck waiting on CI."""

import asyncio
import json
import os
import re
import time
from copy import deepcopy
from dataclasses import asdict

from ai_agent.blockage_ci import superseding_checks
from ai_agent.config import CHAT_ID
from ai_agent.context_attempts import unfinished
from ai_agent.context_models import Scope
from ai_agent.context_recovery import _recovery_lease, _restore_attempt
from ai_agent.context_store import ContextStore, read_bytes
from ai_agent.projects import get_project
from ai_agent.task_pulls import pull_facts
from ai_agent.task_reconcile import _binding, _restore_edges
from ai_agent.tasks import can_resolve_blockage, set_stage


def _idle(task: dict, data: dict) -> None:
    if not can_resolve_blockage(task, data):
        raise ValueError(
            "Only an idle task with saved PR execution evidence can be resolved"
        )
    pending = data.get("pending_plan")
    if task.get("plan_id") and task["plan_id"] == getattr(pending, "id", None):
        raise ValueError("Inspect the pending plan before resolving this task")
    for run in [
        *(data.get("task_queue") or []),
        data.get("pending_implementation") or {},
    ]:
        if (
            run.get("checkpoint_task") == task["id"]
            or run.get("task_id") == task["id"]
            or run.get("branch", run.get("branch_name")) == task["branch"]
        ):
            raise ValueError("This task still has queued or pending execution")


def _state(task: dict) -> dict:
    """Find the exact original run spec, including replay after a persistence gap."""
    spec = task["run"]["context_spec"]
    project = get_project(task["repo"])
    identity = {
        "project_name": task["repo"],
        "repo_path": str(project.repo_path),
        "github_repository": project.github_repository,
    }
    if (
        spec.get("task_id") != task["id"]
        or spec.get("scope") != {"owner_id": str(CHAT_ID), "repository": identity}
        or not spec.get("attempt_id")
    ):
        raise ValueError("Saved execution ownership or repository registration changed")
    store = ContextStore(Scope(str(CHAT_ID)))
    matches = []
    with store.directory() as directory:
        names = os.listdir(directory)
        if len(names) > 100_000:
            raise ValueError("Recovery journal limit exceeded")
        for name in names:
            if not re.fullmatch(r"lifecycle-[0-9a-f]{32}\.json", name):
                continue
            state = json.loads(read_bytes(directory, name, 512 * 1024))
            if state.get("spec") == spec and state.get("branch") == task["branch"]:
                if name != f"lifecycle-{state.get('attempt_id')}.json":
                    raise ValueError(
                        "Attempt journal identity does not match its filename"
                    )
                matches.append({**state, "journal": name})
    if len(matches) != 1 or matches[0].get("schema_version") != 1:
        raise ValueError("Missing or ambiguous identity-bound attempt journal")
    state = matches[0]
    if state.get("terminal") and not (
        state["terminal"] == "succeeded" and "ci_resolution" in state.get("latest", {})
    ):
        raise ValueError("This attempt already has a different immutable result")
    return state


def _latest(state: dict, stage: str) -> dict:
    return state["events"].get(state.get("latest", {}).get(stage), {}).get("facts", {})


def _verify(task: dict, state: dict) -> tuple[dict, dict]:
    identity = state["spec"]["scope"]["repository"]
    pr, commit, ci = (_latest(state, stage) for stage in ("pr", "commit", "ci"))
    if pr.get("url") != task["pr_url"] or pr.get("sha") != commit.get("sha"):
        raise ValueError("Saved PR and final commit evidence do not match")
    facts = pull_facts(identity["github_repository"], pr["url"])
    if (
        not facts["merged"]
        or facts["head"] != commit.get("sha")
        or not re.fullmatch(r"[0-9a-f]{40}", str(facts.get("merge_commit", "")))
    ):
        raise ValueError("The exact saved PR head must be confirmed merged")
    evidence = superseding_checks(identity["github_repository"], facts, ci.get("url"))
    return facts, evidence


async def resolve(task: dict, data: dict) -> str:
    """Verify newer CI, durably finish this attempt, then restore Done only."""
    if task.get("stage") == "done" and task.get("blockage_resolution"):
        return "This blockage was already resolved; the task remains Done."
    _idle(task, data)
    binding = _binding(task, data)
    state = _state(task)
    facts, evidence = await asyncio.to_thread(_verify, deepcopy(task), state)
    _idle(task, data)
    if binding != _binding(task, data) or not any(
        t is task for t in data.get("tasks", [])
    ):
        raise ValueError(
            "Task or dependent work changed during verification; check again"
        )
    identity = state["spec"]["scope"]["repository"]
    # No awaits in this short commit section. Exclude execution and recovery
    # writers and compare the journal again before recording the owner decision.
    with _recovery_lease(identity):
        if _state(task) != state:
            raise ValueError(
                "Attempt evidence changed during verification; check again"
            )
        attempt = _restore_attempt(state, data)
        if not state.get("terminal"):
            attempt.checkpoint(
                "ci_resolution",
                {
                    "state": "superseded",
                    "owner_id": str(CHAT_ID),
                    "url": evidence["verified_checks"][0]["url"],
                    "sha": evidence["verified_checks"][0]["sha"],
                    **evidence,
                },
                required=True,
            )
            attempt.finish("succeeded")
            if attempt.state.get("terminal") != "succeeded":
                raise ValueError(
                    "Resolution could not be archived; recovery remains blocked"
                )
        ref = {
            "kind": "attempt",
            "record_id": state["attempt_id"],
            "revision": 1,
            "scope": asdict(attempt.request.scope),
        }
        task["context_handoff"] = ref
        task["blockage_resolution"] = {
            "at": time.time(),
            "owner_id": str(CHAT_ID),
            "attempt": deepcopy(ref),
            **evidence,
        }
        task.update(
            merged_at=facts["merged_at"],
            merge_commit=facts["merge_commit"],
            autopilot=False,
        )
        task.pop("start_next", None)
        task.pop("dependency_check", None)
        set_stage(task, "done")
        _restore_edges(task, data, ref)
        # Other corrupt or inaccessible journals must keep the global gate closed.
        data["context_recovery_pending"] = True
        try:
            data["context_recovery_pending"] = bool(unfinished(attempt.store))
        except Exception:
            pass
    return (
        "Blockage resolved: PR merged; original CI remains unresolved, superseded "
        "by verified default-branch checks. Task restored to Done. Deployment is "
        "not asserted; dependent tasks still require fresh prerequisite checks."
        + (
            " Other attempts still require recovery."
            if data["context_recovery_pending"]
            else ""
        )
    )
