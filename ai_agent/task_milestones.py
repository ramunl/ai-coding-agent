"""Turn suggested same-repository milestones into an ordered task chain."""

from collections.abc import MutableMapping
from copy import deepcopy

from ai_agent.config import CHAT_ID
from ai_agent.plan_state import PlanDocument, PlanState
from ai_agent.tasks import add_task, tasks_of


def split_milestones(
    data: MutableMapping,
    plan: PlanState,
    document: PlanDocument,
    repo: str,
    identities: dict[str, dict] | None = None,
) -> list[dict]:
    """Replace the planned task while retaining its incoming and outgoing links."""
    source = next(
        (
            task
            for task in tasks_of(data)
            if task.get("plan_id") == plan.id and task.get("stage") == "planned"
        ),
        None,
    )
    milestones = document.scope_assessment["milestones"]
    created = []
    for index, milestone in enumerate(milestones, 1):
        task = add_task(
            data,
            milestone["title"],
            repo,
            source.get("todo") if source and index == len(milestones) else None,
            plan.images,
        )
        task["text"] = (
            f"Assigned repository: {repo}. Plan only milestone {index} of "
            f"{len(milestones)}: {milestone['title']}\n"
            f"Acceptance: {milestone['acceptance']}\n"
            "Return only the assigned repository in repositories. Other milestones "
            "and prerequisites are handled separately; do not repeat their work.\n"
            f"Whole change (context only): {document.summary}\n"
            f"Original request (context only): {plan.feature}"
        )[:4000]
        task.update(
            group=plan.id,
            part=index,
            parts=len(milestones),
            after=created[-1]["id"] if created else (source or {}).get("after"),
        )
        if identities and repo in identities:
            task["dependency_scope"] = {
                "owner_id": str(CHAT_ID),
                "repository": deepcopy(identities[repo]),
            }
        if created and created[-1].get("dependency_scope"):
            task["dependencies"] = [
                {
                    "task_id": created[-1]["id"],
                    "scope": deepcopy(created[-1]["dependency_scope"]),
                    "policy": "merged_in_checkout",
                    "revision": 1,
                }
            ]
        elif not created and source:
            for key in ("dependencies", "dependency_ids", "dependency_invalidations"):
                if key in source:
                    task[key] = deepcopy(source[key])
        if source and source.get("autopilot"):
            task["autopilot"] = True
        created.append(task)
    if source:
        for task in tasks_of(data):
            if task.get("after") == source["id"]:
                task["after"] = created[-1]["id"]
            if isinstance(task.get("dependency_ids"), list):
                task["dependency_ids"] = [
                    created[-1]["id"] if value == source["id"] else value
                    for value in task["dependency_ids"]
                ]
            for edge in task.get("dependencies", []):
                if edge.get("task_id") == source["id"]:
                    edge["task_id"] = created[-1]["id"]
                    edge["scope"] = deepcopy(
                        created[-1].get("dependency_scope", edge["scope"])
                    )
                    edge["revision"] = edge.get("revision", 1) + 1
                    edge.pop("attempt", None)
                    edge.pop("override", None)
        tasks_of(data).remove(source)
    return created
