"""User-visible size advice preserves legacy plans and never schedules work."""

import json

from ai_agent.plan_scope import assess_scope
from ai_agent.plan_state import new_plan_state, plan_outline, render_plan


def test_small_legacy_plan_has_no_warning():
    state = new_plan_state(
        "fix label", json.dumps({"steps": ["Fix label", "Test label"]})
    )
    assert not plan_outline(state)["scope_assessment"]["recommend_split"]
    assert "Scope warning" not in render_plan(state)
    assert not state.approved


def test_large_same_repository_plan_has_concrete_milestones_before_approval():
    advice = {
        "recommend_split": True,
        "reason": "Storage, worker integration and dashboard need separate checks.",
        "milestones": [
            {
                "title": "Persist checkpoints",
                "acceptance": "Restart reloads checkpoint",
            },
            {"title": "Resume worker", "acceptance": "Resume preserves existing edits"},
        ],
    }
    state = new_plan_state("resume", json.dumps({"scope_assessment": advice}))
    outline = plan_outline(state)
    assert outline["scope_assessment"] == advice
    rendered = render_plan(state)
    assert rendered.index("Scope warning") < rendered.index("- /approve")
    assert "Restart reloads checkpoint" in rendered
    assert "/discuss" in rendered
    assert not state.approved


def test_legacy_large_plan_gets_read_only_fallback():
    steps = [f"Implement component {i}" for i in range(8)]
    result = assess_scope(None, [], steps)
    assert result["recommend_split"]
    assert len(result["milestones"]) == 4
    assert "component 0" in result["milestones"][0]["title"]
    assert "heuristic" in result["reason"]


def test_malformed_assessment_is_bounded_and_redacted():
    result = assess_scope(
        {
            "recommend_split": True,
            "reason": "x" * 1000,
            "milestones": [None, {"title": "a" * 400, "acceptance": "b" * 1000}] * 9,
        },
        [],
        [],
    )
    assert len(result["reason"]) == 500
    assert len(result["milestones"]) <= 5
    assert all(
        len(m["title"]) <= 200 and len(m["acceptance"]) <= 500
        for m in result["milestones"]
    )
    assert not assess_scope({"recommend_split": "false"}, [], [])["recommend_split"]


def test_scope_remains_optional_in_schema():
    from ai_agent.planning_provider import SCHEMAS_DIR

    schema = json.loads((SCHEMAS_DIR / "feature_plan.json").read_text())
    assert "scope_assessment" not in schema["required"]
    assert "scope_assessment" in schema["properties"]


def test_compact_same_repo_lifecycle_plan_gets_warning_without_provider_advice():
    result = assess_scope(
        None,
        ["worker.py", "storage.py"],
        ["Persist handoff", "Wire execution", "Update retry", "Test restart"],
        ["Cancellation must remain safe"],
    )
    assert result["recommend_split"]
    assert len(result["milestones"]) == 2
    assert "Persist handoff" in result["milestones"][0]["title"]
