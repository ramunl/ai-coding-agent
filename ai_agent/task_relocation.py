"""Owner-requested correction of an unstarted split child's repository."""

from collections.abc import MutableMapping
from copy import deepcopy

from ai_agent.config import CHAT_ID
from ai_agent.plan_state import PlanDocument, PlanState
from ai_agent.tasks import set_stage, tasks_of


def relocate_part(
    data: MutableMapping,
    source: dict,
    plan: PlanState,
    document: PlanDocument,
    target: dict,
    identities: dict[str, dict] | None,
) -> None:
    """Keep task IDs and chain links; discard the plan bound to the wrong repo."""
    from ai_agent.task_split import part_text

    repo = target["name"]
    if (
        source.get("run")
        or source.get("context_handoff")
        or source.get("attempt_history")
    ):
        raise ValueError(
            "This part has execution history; reconcile it before moving repositories."
        )
    if not identities or repo not in identities:
        raise ValueError("Cannot verify the target repository identity; reload Setup.")
    dependents = [
        t
        for t in tasks_of(data)
        if t.get("after") == source["id"]
        or any(e.get("task_id") == source["id"] for e in t.get("dependencies", []))
    ]
    if any(
        t.get("stage") != "todo" or t.get("run") or t.get("context_handoff")
        for t in dependents
    ):
        raise ValueError(
            "A dependent part has started; reconcile it before moving repositories."
        )
    scope = {"owner_id": str(CHAT_ID), "repository": deepcopy(identities[repo])}
    source["repo"] = repo
    source["text"] = part_text(plan.feature, document, 1, target)
    source["dependency_scope"] = scope
    for key in ("plan_id", "branch", "dependency_check", "worker_failed"):
        source.pop(key, None)
    set_stage(source, "todo", "repository corrected; start planning in " + repo)
    for task in dependents:
        task.pop("dependency_check", None)
        for edge in task.get("dependencies", []):
            if edge.get("task_id") == source["id"]:
                edge["scope"] = deepcopy(scope)
                edge["revision"] = edge.get("revision", 1) + 1
                edge.pop("attempt", None)
                edge.pop("override", None)
