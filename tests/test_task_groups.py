"""Split parent titles remain stable without modifying task identities or history."""

from copy import deepcopy

import pytest

from ai_agent import task_split
from ai_agent.plan_document import parse_plan_document
from ai_agent.task_groups import group_title, split_title
from ai_agent.tasks import view
from tests.test_task_split import KNOWN, _plan


def test_original_parent_title_is_preserved_on_every_repository_part():
    plan = _plan()
    data = {
        "tasks": [
            {
                "id": "parent",
                "plan_id": plan.id,
                "stage": "planned",
                "title": "Original parent",
                "repo": "ai-pm-agent",
                "images": [],
            }
        ]
    }
    parts = task_split.split_plan(
        data, plan, parse_plan_document(plan.plan_text), KNOWN
    )
    assert [part["group_title"] for part in parts] == ["Original parent"] * 2
    assert parts[1]["after"] == parts[0]["id"]
    assert len({part["id"] for part in parts}) == 2
    assert parts[0]["group"] == plan.id


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Assigned repository: repo.\nOriginal request (context only): [DS16] Stage integration\nPrivate additional text",
            "[DS16] Stage integration",
        ),
        ("Whole change (context only): Stage integration\nNext", "Stage integration"),
        ("Whole change: Add type\nOther parts: repo", "Add type"),
    ],
)
def test_legacy_titles_recover_from_saved_parent_context_without_exposing_full_text(
    text, expected
):
    task = {"group": "g", "title": "Milestone", "text": text}
    before = deepcopy(task)
    assert group_title(task) == expected
    assert task == before


def test_legacy_repeated_title_and_missing_parent_metadata():
    assert (
        group_title({"group": "g", "title": "Add type (ai-dashboard, part 2/3)"})
        == "Add type"
    )
    assert group_title({"group": "g", "title": "Milestone only"}) is None
    assert group_title({"title": "Solo", "group_title": "unrelated"}) is None
    assert (
        group_title(
            {
                "group": "g",
                "group_title": "Saved original",
                "text": "Whole change: Different",
            }
        )
        == "Saved original"
    )
    assert (
        split_title("feature", {"group_title": "Original", "title": "Child"})
        == "Original"
    )
    assert len(split_title("a" * 500)) == 300


def test_group_title_is_an_additive_bounded_snapshot_field():
    task = {
        "id": "first",
        "title": "Milestone",
        "stage": "todo",
        "group": "g",
        "part": 1,
        "parts": 2,
        "text": "Whole change: Parent title\nOther parts: secret detail",
        "images": [],
    }
    result = view(task, {"tasks": [task]})
    assert result["group_title"] == "Parent title"
    assert "text" not in result
    assert (result["id"], result["part"], result["parts"]) == ("first", 1, 2)
