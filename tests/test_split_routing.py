"""Cross-repository milestones and correction of wrongly assigned children."""

import json
from copy import deepcopy

import pytest

from ai_agent import task_split, tasks
from ai_agent.plan_state import PlanState, parse_plan_document

KNOWN = {"ai-dashboard", "ai-pm-agent", "ai-ops-agent"}
IDENTITIES = {
    name: {
        "project_name": name,
        "repo_path": "/repos/" + name,
        "github_repository": "owner/" + name,
    }
    for name in KNOWN
}


def plan(milestones, repositories=None, accounting=None):
    return PlanState(
        id="p1",
        feature="Verify baselines",
        revision=1,
        approved=False,
        plan_text=json.dumps(
            {
                "branch": "feature/checks",
                "summary": "Verify baselines",
                "files": [],
                "steps": [],
                "risks": [],
                "repositories": repositories or [],
                "repository_accounting": accounting or [],
                "scope_assessment": {
                    "recommend_split": True,
                    "reason": "Separate baselines",
                    "milestones": milestones,
                },
            }
        ),
    )


def split(data, pending, active="ai-dashboard"):
    return task_split.split_plan(
        data, pending, parse_plan_document(pending.plan_text), KNOWN, active, IDENTITIES
    )


def test_cross_repo_milestones_use_actual_owners_and_dependency_scope():
    pending = plan(
        [
            {"title": "ai-pm-agent baseline verification", "acceptance": "Checks pass"},
            {
                "title": "Verify Ops",
                "acceptance": "Checks pass",
                "repository": "ai-ops-agent",
            },
            {
                "title": "Verify UI",
                "acceptance": "Checks pass",
                "repository": "ai-dashboard",
            },
        ]
    )
    first, second, third = split({}, pending)
    assert [t["repo"] for t in (first, second, third)] == [
        "ai-pm-agent",
        "ai-ops-agent",
        "ai-dashboard",
    ]
    assert first["text"].startswith("Assigned repository: ai-pm-agent.")
    assert second["after"] == first["id"]
    assert second["dependencies"][0]["scope"] == first["dependency_scope"]
    assert third["dependencies"][0]["scope"] == second["dependency_scope"]


@pytest.mark.parametrize(
    "milestones",
    [
        [
            {"title": "Verify PM", "repository": "unknown", "acceptance": "Pass"},
            {"title": "UI", "acceptance": "Pass"},
        ],
        [
            {
                "title": "ai-pm-agent checks",
                "repository": "ai-dashboard",
                "acceptance": "Pass",
            },
            {"title": "UI", "acceptance": "Pass"},
        ],
        [
            {"title": "Run checks", "acceptance": "Pass"},
            {"title": "Record evidence", "acceptance": "Pass"},
        ],
    ],
)
def test_invalid_or_ambiguous_owners_do_not_create_partial_tasks(milestones):
    data = {}
    pending = plan(milestones, accounting=[{"name": "ai-pm-agent", "role": "verify"}])
    with pytest.raises(task_split.SplitError):
        split(data, pending)
    assert not tasks.tasks_of(data)


@pytest.mark.parametrize("active", ["ai-dashboard", "ai-pm-agent"])
def test_move_child_keeps_identity_and_links_but_requires_replanning(active):
    data = {}
    source = tasks.add_task(data, "PM baseline", "ai-dashboard", "project:todo")
    source.update(
        stage="planned",
        plan_id="p1",
        group="group",
        part=1,
        parts=3,
        after="previous",
        autopilot=True,
        branch="feature/checks",
    )
    child = tasks.add_task(data, "Ops baseline", "ai-ops-agent", None)
    child.update(
        after=source["id"],
        dependencies=[
            {
                "task_id": source["id"],
                "scope": {"old": True},
                "revision": 1,
                "policy": "merged_in_checkout",
                "attempt": {"old": True},
            }
        ],
    )
    pending = plan([], [{"name": "ai-pm-agent", "changes": "Verify baseline"}])
    (moved,) = split(data, pending, active)
    assert moved is source
    assert len(tasks.tasks_of(data)) == 2
    assert source["repo"] == "ai-pm-agent" and source["stage"] == "todo"
    assert source["after"] == "previous" and source["todo"] == "project:todo"
    assert source["group"] == "group" and source["autopilot"]
    assert "plan_id" not in source and "branch" not in source
    assert child["after"] == source["id"]
    assert child["dependencies"][0]["scope"] == source["dependency_scope"]
    assert "attempt" not in child["dependencies"][0]
    assert source["text"].startswith("Assigned repository: ai-pm-agent.")


@pytest.mark.parametrize("history", ["run", "context_handoff", "attempt_history"])
def test_move_refuses_executed_children_without_mutation(history):
    data = {}
    source = tasks.add_task(data, "PM baseline", "ai-dashboard", None)
    source.update(stage="planned", plan_id="p1", group="group")
    source[history] = {"evidence": True}
    before = deepcopy(source)
    with pytest.raises(task_split.SplitError, match="execution history"):
        split(data, plan([], [{"name": "ai-pm-agent", "changes": "Verify baseline"}]))
    assert source == before


def test_move_refuses_started_dependent_without_mutation():
    data = {}
    source = tasks.add_task(data, "PM baseline", "ai-dashboard", None)
    source.update(stage="planned", plan_id="p1", group="group")
    dependent = tasks.add_task(data, "Ops", "ai-ops-agent", None)
    dependent.update(stage="planning", after=source["id"])
    before = deepcopy(data)
    with pytest.raises(task_split.SplitError, match="dependent part has started"):
        split(data, plan([], [{"name": "ai-pm-agent", "changes": "Verify baseline"}]))
    assert data == before
