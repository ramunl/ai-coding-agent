"""Split a plan that spans several repositories into one task per repository.

The agent runs one repository at a time. A plan that lists several (see
ecosystem.py) becomes a chain of To do tasks, one per repository, in the
plan's order: each is planned in its own repository, with the whole change as
context, and gets its own branch and pull request. A part waits until the
part before it has a pull request (or is merged), so storage and API changes
come before the screens that use them. The todo the plan came from goes with
the last part, so it is closed only when the whole change is deployed.
"""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping

from ai_agent.plan_state import PlanDocument, PlanState, parse_plan_document
from ai_agent.projects import active_project, list_projects
from ai_agent.tasks import add_task, tasks_of

TEXT_LIMIT = 4000


class SplitError(ValueError):
    """The plan cannot be split; the message says why."""


def part_text(
    feature: str, document: PlanDocument, index: int, repository: dict
) -> str:
    """What a part's planner gets: its own part, with the whole change around it."""
    parts = document.repositories
    others = "; ".join(
        f"{number}. {item['name']}: {item['changes']}"
        for number, item in enumerate(parts, 1)
        if item is not repository
    )
    if len(parts) == 1:
        return (
            f"Assigned repository: {repository['name']}. This task is already scoped. "
            "Return only this repository in repositories; do not split it again.\n"
            f"In this repository: {repository['changes']}\n\n"
            f"Original request (context only): {feature}"
        )[:TEXT_LIMIT]
    text = (
        f"Assigned repository: {repository['name']}. This task is already split. "
        "Return only this repository in repositories; do not split it again.\n"
        f"This is part {index} of {len(parts)} of a change across several "
        f"repositories. Plan only this repository's part: {repository['changes']}\n"
        "Other repositories are context and prerequisites only; mention them in "
        "risks if needed, never as changes or steps in this plan.\n"
        f"Whole change: {document.summary}\n"
        f"Other parts (done separately, in this order): {others}\n\n"
        f"Original request (context only): {feature}"
    )
    return text[:TEXT_LIMIT]


def split_plan(
    data: MutableMapping,
    plan: PlanState,
    document: PlanDocument,
    known: Iterable[str],
    active: str = "",
) -> list[dict]:
    """Add one To do task per repository of the plan; return them in order.

    A plan for one repository that isn't the active one becomes one task
    there (planned again in that repository), so a task made in the wrong
    project can be moved instead of revised.
    """
    parts = document.repositories
    if not parts or (len(parts) == 1 and parts[0]["name"] == active):
        raise SplitError("This plan needs only this repository; approve it instead.")
    unknown = [item["name"] for item in parts if item["name"] not in set(known)]
    if unknown:
        raise SplitError(
            f"Not registered: {', '.join(unknown)}. Add the repository in Setup, "
            "or revise the plan."
        )
    source = next(
        (
            task
            for task in tasks_of(data)
            if task.get("plan_id") == plan.id and task.get("stage") == "planned"
        ),
        None,
    )
    if source is not None and source.get("group"):
        raise SplitError(
            "This task is already a repository part. Revise its plan to change "
            f"only {source['repo']}; other parts are handled separately."
        )
    todo = source.get("todo") if source else None
    created: list[dict] = []
    for index, repository in enumerate(parts, 1):
        is_last = index == len(parts)
        task = add_task(
            data,
            plan.feature,
            repository["name"],
            todo if is_last else None,
            plan.images,
        )
        suffix = f", part {index}/{len(parts)}" if len(parts) > 1 else ""
        task["title"] = f"{task['title']} ({repository['name']}{suffix})"
        task["text"] = part_text(plan.feature, document, index, repository)
        if source is not None and source.get("autopilot"):
            task["autopilot"] = True  # an Autopilot task's parts stay on autopilot
        task.update(
            group=plan.id,
            part=index,
            parts=len(parts),
            after=created[-1]["id"] if created else None,
        )
        created.append(task)
    if source is not None:
        tasks_of(data).remove(source)
    return created


def split_pending(data: MutableMapping) -> str:
    """Split the pending plan into tasks and drop it; return the chat reply."""
    plan = data.get("pending_plan")
    if plan is None:
        raise SplitError("No pending plan to split.")
    if data.get("planning_task") or data.get("thinking"):
        raise SplitError("Wait until the agent finishes planning.")
    document = parse_plan_document(plan.plan_text, plan.feature)
    created = split_plan(
        data,
        plan,
        document,
        [project.name for project in list_projects()],
        active_project().name,
    )
    data.pop("pending_plan", None)
    data.pop("pending_implementation", None)
    if len(created) == 1:
        return (
            f"Moved to {created[0]['repo']}: a To do task that is planned again "
            "there. Start it in Tasks (or let the worker take it)."
        )
    lines = [f"Split into {len(created)} tasks, one per repository:"]
    lines.extend(f"{task['part']}. {task['repo']}" for task in created)
    lines.append(
        "Each is planned in its own repository. Part 1 can start now (Tasks › "
        "Start planning, or the worker); each next part waits for the pull "
        "request of the one before it."
    )
    return "\n".join(lines)
