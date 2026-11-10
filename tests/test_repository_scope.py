"""All providers must account for explicitly requested repositories."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ai_agent import repository_scope as scope
from ai_agent.context_lifecycle import approve_plan
from ai_agent.context_sources import ContextBlockedError
from ai_agent.plan_state import (
    new_plan_state,
    parse_plan_document,
    plan_outline,
    revise_plan_state,
)

NAMES = ["ai-coding-agent", "ai-pm-agent", "ai-ops-agent", "ai-dashboard"]


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(
        scope, "list_projects", lambda: [SimpleNamespace(name=n) for n in NAMES]
    )


def plan(rows=(), repositories=None, steps=(), feature=None, profile=""):
    return new_plan_state(
        feature or "Repos: " + ", ".join(NAMES),
        json.dumps(
            {
                "summary": "baseline checks",
                "verification_profile": profile,
                "branch": "feature/check",
                "files": [],
                "steps": list(steps),
                "repositories": repositories
                if repositories is not None
                else [{"name": NAMES[0], "changes": "Add baseline script"}],
                "repository_accounting": list(rows),
            }
        ),
        {"project_name": profile.partition(":")[0]} if profile else None,
    )


def test_ds01_cannot_silently_narrow_four_repositories_to_one():
    p = plan()
    assert len(scope.review(p)) == 3
    with pytest.raises(ContextBlockedError, match="ai-pm-agent"):
        approve_plan(p)
    assert not p.approved
    assert "Repository scope review required" in " ".join(plan_outline(p)["risks"])


def test_verification_only_repositories_do_not_force_split():
    p = plan(
        [
            {"name": name, "role": "verify", "reason": "Run baseline tests"}
            for name in NAMES[1:]
        ],
        steps=[f"Run offline baseline checks in {name}" for name in NAMES[1:]],
    )
    assert scope.review(p) == []
    document = parse_plan_document(p.plan_text)
    assert len(document.repositories) == 1
    assert not document.scope_assessment["recommend_split"]
    approve_plan(p)
    assert p.approved


def test_multiple_implementation_owners_retain_split_input():
    p = plan(
        repositories=[
            {"name": name, "changes": "Implement the repository part"} for name in NAMES
        ]
    )
    assert scope.review(p) == []
    assert len(plan_outline(p)["repositories"]) == 4


@pytest.mark.parametrize(
    "role", ["unclear", "completed", "context", "verify", "change"]
)
def test_unsupported_exclusions_and_missing_evidence_block(role):
    p = plan([{"name": name, "role": role} for name in NAMES[1:]])
    assert scope.review(p)


def test_completed_requires_explicit_dated_evidence():
    rows = [
        {
            "name": name,
            "role": "completed",
            "reason": "Previously verified",
            "evidence": "Readiness receipt DS01",
            "checked_at": "2026-10-30",
        }
        for name in NAMES[1:]
    ]
    p = plan(rows)
    assert scope.review(p) == []
    rows[0]["checked_at"] = "yesterday"
    assert scope.review(plan(rows))
    rows[0]["checked_at"] = "9999-01-01"
    assert scope.review(plan(rows))


def test_scoped_child_does_not_expand_parent_repository_list():
    p = plan(
        feature="Assigned repository: ai-coding-agent. This task is already split. Other repositories (context): ai-pm-agent, ai-ops-agent, ai-dashboard."
    )
    assert scope.review(p) == []
    p = plan(
        feature=p.feature,
        repositories=[{"name": n, "changes": "Change"} for n in NAMES],
    )
    assert scope.review(p)


def test_context_requires_explicit_explanation_and_survives_plan_rewrites():
    rows = [
        {
            "name": n,
            "role": "context",
            "reason": "Only the audit target; no implementation requested",
        }
        for n in NAMES[1:]
    ]
    p = plan(rows)
    assert scope.review(p) == []
    parsed = parse_plan_document(p.plan_text)
    p.plan_text = json.dumps(asdict(parsed))
    assert scope.review(p) == []
    revised = revise_plan_state(p, plan().plan_text)
    with pytest.raises(ContextBlockedError):
        approve_plan(revised)


def test_repository_matching_uses_names_not_substrings():
    assert scope.review(plan(feature="Change ai-coding-agent-extra only")) == []


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_both_planners_request_accounting_and_cannot_approve_omissions(
    monkeypatch, provider
):
    from ai_agent import planner

    monkeypatch.setattr(
        planner,
        "active_project",
        lambda: SimpleNamespace(github_repository="owner/ai-coding-agent"),
    )
    for name in [
        "repo_file_sample",
        "project_brief_block",
        "ecosystem_block",
        "rules_prompt_block",
    ]:
        monkeypatch.setattr(planner, name, lambda *a, **k: "")
    monkeypatch.setattr(planner, "enrich_feature_description", lambda text: text)
    # This test is about repository accounting, not live context, so bypass the
    # API planner's context block (which would need a real repo_path here).
    monkeypatch.setattr(planner, "_live_context_block", lambda *a, **k: ("", None))
    prompts = []
    monkeypatch.setattr(
        planner,
        "planner_message",
        lambda prompt, selected, *a, **k: (
            prompts.append((prompt, selected)) or plan().plan_text
        ),
    )
    feature = "Change ai-coding-agent and ai-pm-agent"
    output = planner.plan_feature(feature, provider)
    assert "repository_accounting" in prompts[0][0]
    assert prompts[0][1] == provider
    with pytest.raises(ContextBlockedError, match="ai-pm-agent"):
        approve_plan(new_plan_state(feature, output))


def test_provider_schema_accepts_and_requires_accounting():
    from ai_agent.planning_provider import SCHEMAS_DIR

    schema = json.loads((SCHEMAS_DIR / "feature_plan.json").read_text())
    assert "repository_accounting" in schema["required"]
    assert schema["properties"]["repository_accounting"]["items"]["properties"]["role"][
        "enum"
    ] == ["change", "verify", "completed", "context", "unclear"]


def test_direct_multi_repository_bugfix_cannot_bypass_scope_review(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from ai_agent.bot import planning

    reply = AsyncMock()
    monkeypatch.setattr(planning, "reply_chunks", reply)
    context = SimpleNamespace(user_data={})
    asyncio.run(
        planning.prepare_bugfix(
            None, context, "Fix ai-coding-agent and ai-dashboard", "fix"
        )
    )
    assert "Use /plan" in reply.call_args.args[1]
    assert "pending_implementation" not in context.user_data


@pytest.mark.parametrize("punctuation", [".", ",", ":", ";", "!"])
def test_repository_name_before_sentence_punctuation_is_accounted(punctuation):
    p = plan(feature="Change ai-coding-agent and ai-dashboard" + punctuation)
    assert any("ai-dashboard" in problem for problem in scope.review(p))


def test_repository_name_does_not_match_dot_suffix():
    assert scope.review(plan(feature="Change ai-dashboard.extra only")) == []


def test_verification_only_child_can_approve_its_execution_repository():
    p = plan(
        [
            {
                "name": "ai-pm-agent",
                "role": "verify",
                "reason": "Run existing offline checks",
            }
        ],
        repositories=[
            {"name": "ai-pm-agent", "changes": "Verification only: run existing checks"}
        ],
        steps=["Run pytest and Ruff in ai-pm-agent; record commit and results"],
        feature="Assigned repository: ai-pm-agent. Verify baseline only.",
        profile="ai-pm-agent:baseline-v1",
    )
    assert scope.review(p) == []
    approve_plan(p)
    assert p.approved

    raw = json.loads(p.plan_text)
    raw["files"] = ["ai_pm_agent/task_service.py"]
    p.plan_text = json.dumps(raw)
    assert any("conflicting" in problem for problem in scope.review(p))


def test_verification_execution_target_still_requires_named_checks_and_reason():
    p = plan(
        [{"name": "ai-pm-agent", "role": "verify", "reason": "Run existing checks"}],
        repositories=[{"name": "ai-pm-agent", "changes": "Verify"}],
        feature="Assigned repository: ai-pm-agent. Verify baseline only.",
    )
    assert any("concrete verification step" in problem for problem in scope.review(p))


def test_verification_accepts_unambiguous_package_spelling_in_saved_plan():
    p = plan(
        [
            {
                "name": "ai-pm-agent",
                "role": "verify",
                "reason": "Run existing offline checks",
            }
        ],
        repositories=[{"name": "ai-pm-agent", "changes": "Verification only"}],
        steps=[
            "Run Python pytest and Ruff",
            "Confirm git diff for ai_pm_agent is empty and report verification results",
        ],
        feature="Assigned repository: ai-pm-agent. Verify only.",
        profile="ai-pm-agent:baseline-v1",
    )
    assert scope.review(p) == []
    approve_plan(p)
    assert p.approved


def test_repository_spelling_does_not_accept_ambiguous_or_partial_matches():
    assert scope._names_repository(
        "Check ai_dashboard", "ai-dashboard", ["ai-dashboard"]
    )
    assert not scope._names_repository(
        "Check ai_dashboard", "ai-dashboard", ["ai-dashboard", "ai_dashboard"]
    )
    assert not scope._names_repository(
        "Check other-ai-dashboard", "ai-dashboard", ["ai-dashboard"]
    )
    assert not scope._names_repository(
        "Check ai-dashboard-extra", "ai-dashboard", ["ai-dashboard"]
    )
    assert scope._names_repository(
        "Check ai-dashboard", "ai-dashboard", ["ai-dashboard", "ai_dashboard"]
    )


def test_repository_spelling_allows_sentence_punctuation_but_not_dot_suffixes():
    assert scope._names_repository(
        "Run checks in ai_dashboard.", "ai-dashboard", ["ai-dashboard"]
    )
    assert not scope._names_repository(
        "Check ai_dashboard.backup", "ai-dashboard", ["ai-dashboard"]
    )
