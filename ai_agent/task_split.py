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
from ai_agent.projects import list_projects
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
    text = (
        f"{feature}\n\n"
        f"This is part {index} of {len(parts)} of a change across several "
        f"repositories. Plan only this repository's part: {repository['changes']}\n"
        f"Whole change: {document.summary}\n"
        f"Other parts (done separately, in this order): {others}"
    )
    return text[:TEXT_LIMIT]


def split_plan(
    data: MutableMapping,
    plan: PlanState,
    document: PlanDocument,
    known: Iterable[str],
) -> list[dict]:
    """Add one To do task per repository of the plan; return them in order."""
    parts = document.repositories
    if len(parts) < 2:
        raise SplitError("This plan needs only one repository; approve it instead.")
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
    todo = source.get("todo") if source else None
    created: list[dict] = []
    for index, repository in enumerate(parts, 1):
        is_last = index == len(parts)
        task = add_task(
            data, plan.feature, repository["name"], todo if is_last else None
        )
        task["title"] = (
            f"{task['title']} ({repository['name']}, part {index}/{len(parts)})"
        )
        task["text"] = part_text(plan.feature, document, index, repository)
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
        data, plan, document, [project.name for project in list_projects()]
    )
    data.pop("pending_plan", None)
    data.pop("pending_implementation", None)
    lines = [f"Split into {len(created)} tasks, one per repository:"]
    lines.extend(f"{task['part']}. {task['repo']}" for task in created)
    lines.append(
        "Each is planned in its own repository. Part 1 can start now (Tasks › "
        "Start planning, or the worker); each next part waits for the pull "
        "request of the one before it."
    )
    return "\n".join(lines)
