"""Regression for DS15's npm-only plan falling back to a paid editing run."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent import context_lifecycle, repository_scope, verification
from ai_agent.context_sources import ContextBlockedError
from ai_agent.plan_document import parse_plan_document
from ai_agent.plan_state import new_plan_state, plan_outline, render_plan
from tests.test_verification import environment as environment
from tests.test_verification import receipt

PAGE_PROFILE = "ai-dashboard:page-tests-v1"
IDENTITY = {
    "project_name": "ai-dashboard",
    "repo_path": "/opt/projects/ai-dashboard",
    "github_repository": "owner/ai-dashboard",
}


def npm_plan(profile=""):
    """The material shape of the failed plan, without its model success claims."""
    return {
        "branch": "verify/fake-provider",
        "summary": "Verification only: confirm existing fake-provider page tests.",
        "verification_profile": profile,
        "files": [],
        "steps": [
            "Run npm test in /opt/projects/ai-dashboard.",
            "Confirm registry.test.js renders the existing third-provider fixture.",
            "Confirm the page-test suite exits 0.",
        ],
        "repositories": [{"name": "ai-dashboard", "changes": "Verification only"}],
        "repository_accounting": [
            {
                "name": "ai-dashboard",
                "role": "verify",
                "reason": "Assigned verification",
            },
            {
                "name": "ai-coding-agent",
                "role": "context",
                "reason": "Merged prerequisite",
            },
        ],
    }


def state(profile=""):
    return new_plan_state(
        "Assigned repository: ai-dashboard. Verification only, npm test.",
        json.dumps(npm_plan(profile)),
        IDENTITY,
    )


def test_empty_profile_no_longer_falls_back_to_implementation():
    document = parse_plan_document(json.dumps(npm_plan()))
    with pytest.raises(ValueError, match="no execution profile"):
        verification.approved_profile(document, IDENTITY)
    outline = plan_outline(state())
    assert "no execution profile" in outline["steps"][0]
    assert PAGE_PROFILE in outline["steps"][0]
    assert "cannot run as an implementation" in render_plan(state())


def test_missing_profile_rejects_approval_and_old_pending_approval(monkeypatch):
    monkeypatch.setattr(
        repository_scope,
        "list_projects",
        lambda: [SimpleNamespace(name="ai-dashboard")],
    )
    plan = state()
    with pytest.raises(ContextBlockedError, match="no execution profile"):
        context_lifecycle.approve_plan(plan)
    assert not plan.approved
    # Old saved boolean approvals must not reopen the paid fallback either.
    plan.approved = True
    with pytest.raises(ContextBlockedError, match="no execution profile"):
        context_lifecycle.require_plan_approval(plan)
    assert not plan.approved
    assert plan.review_required


def test_page_profile_is_explicitly_approved_and_visible(monkeypatch):
    monkeypatch.setattr(
        repository_scope,
        "list_projects",
        lambda: [SimpleNamespace(name="ai-dashboard")],
    )
    plan = state(PAGE_PROFILE)
    context_lifecycle.approve_plan(plan)
    context_lifecycle.require_plan_approval(plan)
    assert plan.approved
    document = parse_plan_document(plan.plan_text)
    assert verification.approved_profile(document, IDENTITY) == PAGE_PROFILE
    assert verification.commands_for(PAGE_PROFILE) == (("npm", "test"),)
    steps = plan_outline(plan)["steps"]
    assert "no model or PR" in steps[0]
    assert steps[1] == "npm test"
    with pytest.raises(ValueError, match="matching repository"):
        verification.approved_profile(
            document, {**IDENTITY, "project_name": "ai-pm-agent"}
        )


def test_ordinary_implementation_keeps_empty_profile_compatibility():
    value = npm_plan()
    value.update(
        files=["ai_dashboard/page.py"],
        repository_accounting=[{"name": "ai-dashboard", "role": "change"}],
    )
    assert (
        verification.approved_profile(parse_plan_document(json.dumps(value)), IDENTITY)
        == ""
    )


def test_npm_only_run_records_npm_receipt_without_python_or_model(environment):
    env = environment
    verification.run_verification.__wrapped__(PAGE_PROFILE)
    assert [argv for argv, _ in env.calls] == [["npm", "test"]]
    record = receipt(env)
    evidence = next(
        item for item in record.evidence if item.locator == "verification:v1"
    )
    facts = json.loads(evidence.summary)
    assert facts["profile"] == PAGE_PROFILE
    assert facts["checks"] == [{"command": ["npm", "test"], "exit_code": 0}]
    assert verification.verify_receipt(record) == env.git("rev-parse", "HEAD")
    # Calling npm alone cannot masquerade as a full Python/Ruff baseline.
    facts["profile"] = "ai-dashboard:baseline-v1"
    invalid = replace(record, evidence=(replace(evidence, summary=json.dumps(facts)),))
    with pytest.raises(ValueError, match="Invalid application"):
        verification.verify_receipt(invalid)


def test_npm_receipt_satisfies_pinned_dependency_without_any_pr(environment):
    from ai_agent import dependency_evidence
    from ai_agent.context_models import Dependency, RecordReference

    env = environment
    verification.run_verification.__wrapped__(PAGE_PROFILE)
    record = receipt(env)
    edge = asdict(
        Dependency(
            "task",
            record.scope,
            RecordReference("attempt", record.record_id, record.revision, record.scope),
            "verified_in_checkout",
        )
    )
    result = dependency_evidence.evaluate_dependency(edge, store=env.attempt.store)
    assert result["checkout_head"] == env.git("rev-parse", "HEAD")
    assert "pull" not in result


def test_provider_schema_and_guidance_support_npm_only_profile():
    schema = json.loads(
        (Path(verification.__file__).parent / "schemas/feature_plan.json").read_text()
    )
    assert PAGE_PROFILE in schema["properties"]["verification_profile"]["enum"]
    assert PAGE_PROFILE in repository_scope.GUIDANCE
    assert "empty profile is rejected" in repository_scope.GUIDANCE
