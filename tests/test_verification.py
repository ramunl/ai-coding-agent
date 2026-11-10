"""Verification completion depends on supervised commands, never provider prose."""

import asyncio
import json
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_agent import (
    context_attempts,
    context_lifecycle,
    dependency_evidence,
    isolation,
    provider_process,
    shell,
    verification,
)
from ai_agent.context_models import (
    Dependency,
    RecordReference,
)
from ai_agent.context_sources import ContextBlockedError
from ai_agent.context_store import ContextStore
from ai_agent.plan_document import parse_plan_document
from ai_agent.run_outcomes import NoCodeOutcomeError

PROFILE = "ai-dashboard:baseline-v1"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.org")
    git("config", "user.name", "Test")
    (repo / ".gitignore").write_text("node_modules/\n")
    git("add", ".")
    git("commit", "-qm", "base")
    git("remote", "add", "origin", "https://github.com/owner/ai-dashboard.git")
    (repo / "node_modules").mkdir()
    project = SimpleNamespace(
        name="ai-dashboard",
        repo_path=repo,
        github_repository="owner/ai-dashboard",
        base_branch="main",
    )
    monkeypatch.setattr(verification, "active_project", lambda: project)
    monkeypatch.setattr(shell, "active_project", lambda: project)
    monkeypatch.setattr(isolation, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(isolation, "WORKER_ISOLATION_ENABLED", False)
    monkeypatch.setattr(verification, "WORKER_ISOLATION_ENABLED", False)
    test_python = tmp_path / "test-environment" / "bin" / "python"

    @contextmanager
    def prepared_python(path, commands):
        yield test_python if any(c[0] in {"python", "ruff"} for c in commands) else None

    monkeypatch.setattr(
        verification, "python_environment", prepared_python, raising=False
    )
    command_run = verification.run

    def run(args, **kwargs):
        if args[:2] == ["git", "pull"]:
            return SimpleNamespace(output="")
        return command_run(args, **kwargs)

    monkeypatch.setattr(verification, "run", run)
    monkeypatch.setattr(
        context_lifecycle,
        "ContextStore",
        lambda scope: ContextStore(scope, tmp_path / "archive"),
    )
    spec = context_lifecycle.new_spec(
        str(dependency_evidence.CHAT_ID),
        {
            "project_name": project.name,
            "repo_path": str(repo),
            "github_repository": project.github_repository,
        },
        "task",
    )
    task = {
        "id": "task",
        "repo": project.name,
        "branch": "feature/verify",
        "stage": "implementing",
    }
    data = {"tasks": [task]}
    attempt = context_attempts.Attempt(spec, task["branch"], data, recovering=True)
    attempt.save()
    token = context_attempts.current_attempt.set(attempt)
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="passed", stderr="")

    monkeypatch.setattr(provider_process, "grouped_run", execute)
    yield SimpleNamespace(
        repo=repo,
        project=project,
        git=git,
        spec=spec,
        task=task,
        data=data,
        attempt=attempt,
        calls=calls,
        test_python=test_python,
    )
    context_attempts.current_attempt.reset(token)


def receipt(env):
    env.attempt.finish("succeeded")
    assert not env.attempt.failed
    return env.attempt.store.get("attempt", env.attempt.state["attempt_id"])


def test_pass_records_every_command_and_can_satisfy_dependency(environment):
    env = environment
    verification.run_verification.__wrapped__(PROFILE)
    record = receipt(env)
    assert len(env.calls) == 4
    assert all(kwargs["max_output_chars"] == 8192 for _, kwargs in env.calls)
    assert verification.verify_receipt(record) == env.git("rev-parse", "HEAD")
    edge = asdict(
        Dependency(
            "task",
            record.scope,
            RecordReference("attempt", record.record_id, record.revision, record.scope),
            "verified_in_checkout",
        )
    )
    facts = dependency_evidence.evaluate_dependency(edge, store=env.attempt.store)
    assert facts["checkout_head"] == env.git("rev-parse", "HEAD")
    # The existing PR policy continues requiring a PR.
    edge["policy"] = "merged_in_checkout"
    with pytest.raises(ValueError, match="PR evidence"):
        dependency_evidence.evaluate_dependency(edge, store=env.attempt.store)


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing", "edits"])
def test_failed_incomplete_or_changed_checks_never_record_completion(
    environment, monkeypatch, failure
):
    def fail(argv, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1800)
        if failure == "missing":
            raise FileNotFoundError("ruff")
        if failure == "edits":
            (kwargs["cwd"] / "unexpected.txt").write_text("edits")
        return SimpleNamespace(
            returncode=1 if failure == "exit" else 0, stdout="failed", stderr=""
        )

    monkeypatch.setattr(provider_process, "grouped_run", fail)
    with pytest.raises((NoCodeOutcomeError, ContextBlockedError)):
        verification.run_verification.__wrapped__(PROFILE)
    assert "verification" not in environment.attempt.state.get("latest", {})


@pytest.mark.parametrize(
    "change", ["head", "dirty", "remote", "failed", "missing_check", "prose"]
)
def test_receipt_cannot_authorize_other_state(environment, change):
    env = environment
    verification.run_verification.__wrapped__(PROFILE)
    record = receipt(env)
    if change == "head":
        env.git("commit", "--allow-empty", "-qm", "new head")
    elif change == "dirty":
        (env.repo / "new.txt").write_text("new")
    elif change == "remote":
        env.git("remote", "set-url", "origin", "https://github.com/owner/other.git")
    elif change == "failed":
        record = replace(record, outcome="failed")
    else:
        evidence = next(
            item for item in record.evidence if item.locator == "verification:v1"
        )
        payload = json.loads(evidence.summary)
        payload["checks"] = payload["checks"][:-1]
        summary = (
            json.dumps(payload) if change == "missing_check" else "All tests passed!"
        )
        record = replace(record, evidence=(replace(evidence, summary=summary),))
    with pytest.raises((ValueError, json.JSONDecodeError)):
        verification.verify_receipt(record)


def test_no_code_model_claim_is_still_blocked():
    assert (
        NoCodeOutcomeError('{"outcome":"succeeded","summary":"all passed"}').status
        == "blocked"
    )


def test_profile_must_be_explicit_and_bound_to_approved_repository():
    doc = parse_plan_document(
        json.dumps(
            {
                "verification_profile": PROFILE,
                "repositories": [{"name": "ai-dashboard", "changes": "verify"}],
                "repository_accounting": [{"name": "ai-dashboard", "role": "verify"}],
            }
        )
    )
    assert (
        verification.approved_profile(doc, {"project_name": "ai-dashboard"}) == PROFILE
    )
    for mutated in (
        replace(doc, files=["code.py"]),
        replace(doc, repositories=[]),
        replace(doc, repository_accounting=[]),
    ):
        with pytest.raises(ValueError):
            verification.approved_profile(mutated, {"project_name": "ai-dashboard"})
    with pytest.raises(ValueError):
        verification.commands_for("ai-dashboard:baseline-v1; rm -rf /")
    run = {"verification_profile": PROFILE, "context_spec": {}}
    with pytest.raises(ContextBlockedError):
        context_lifecycle.require_run(run)


def test_profile_is_sealed_without_changing_legacy_bindings():
    item = {"codex_prompt": "approved"}
    old = context_lifecycle.run_binding(item)
    assert context_lifecycle.run_binding({**item, "verification_profile": ""}) == old
    assert (
        context_lifecycle.run_binding({**item, "verification_profile": PROFILE}) != old
    )


@pytest.mark.parametrize("profile", [PROFILE, "ai-dashboard:page-tests-v1"])
def test_lifecycle_completes_only_after_durable_receipt(
    environment, monkeypatch, profile
):
    from ai_agent.bot import context_execution

    env = environment
    monkeypatch.setattr(context_execution, "CHAT_ID", env.spec["scope"]["owner_id"])
    context = SimpleNamespace(user_data=env.data)
    item = {
        "context_spec": env.spec,
        "branch_name": env.task["branch"],
        "verification_profile": profile,
    }
    item["run_binding"] = context_lifecycle.run_binding(item)

    @context_execution.lifecycle_execution
    async def verify(update, context, item):
        verification.run_verification.__wrapped__(profile)

    # The wrapper owns its own fresh journal; settle the fixture journal first.
    env.attempt.finish("blocked")
    asyncio.run(verify(None, context, item))
    assert env.task["stage"] == "done"
    assert env.task["verification_completed"] is True
    assert env.task["context_handoff"]
    assert "pr_url" not in env.task


def test_owner_start_reviews_legacy_edge_without_weakening_deployment_policy(
    environment, monkeypatch
):
    env = environment
    verification.run_verification.__wrapped__(PROFILE)
    record = receipt(env)
    env.task.update(stage="done", verification_completed=True)
    ref = env.task["context_handoff"]
    edge = asdict(
        Dependency(
            "task",
            record.scope,
            RecordReference("attempt", record.record_id, record.revision, record.scope),
        )
    )
    consumer = {"id": "next", "stage": "todo", "dependencies": [edge]}
    env.data["tasks"].append(consumer)
    monkeypatch.setattr(
        dependency_evidence, "ContextStore", lambda scope: env.attempt.store
    )
    assert (
        asyncio.run(dependency_evidence.review_replacements(consumer, env.data)) is None
    )
    assert consumer["dependencies"][0]["policy"] == "verified_in_checkout"
    assert consumer["dependencies"][0]["attempt"] == ref
    assert consumer["dependency_reviews"][-1]["source"] == "start_task"
    assert consumer["stage"] == "todo"
    deployed = {
        "id": "deploy",
        "stage": "todo",
        "dependencies": [{**edge, "policy": "deployed"}],
    }
    asyncio.run(dependency_evidence.review_replacements(deployed, env.data))
    assert deployed["dependencies"][0]["policy"] == "deployed"


def test_storage_failure_never_marks_verification_task_done(environment, monkeypatch):
    from ai_agent.bot import context_execution

    env = environment
    monkeypatch.setattr(context_execution, "CHAT_ID", env.spec["scope"]["owner_id"])
    env.attempt.finish("blocked")
    real_finish = context_attempts.Attempt.finish

    def failed_finish(self, outcome):
        if outcome == "succeeded":
            self.failed = True
        else:
            real_finish(self, outcome)

    monkeypatch.setattr(context_attempts.Attempt, "finish", failed_finish)
    context = SimpleNamespace(user_data=env.data)
    item = {
        "context_spec": env.spec,
        "branch_name": env.task["branch"],
        "verification_profile": PROFILE,
    }
    item["run_binding"] = context_lifecycle.run_binding(item)

    @context_execution.lifecycle_execution
    async def verify(update, context, item):
        verification.run_verification.__wrapped__(PROFILE)

    with pytest.raises(ContextBlockedError, match="receipt"):
        asyncio.run(verify(None, context, item))
    assert env.task["stage"] != "done"
    assert not env.task.get("verification_completed")


def test_coding_profile_covers_documented_baseline_paths():
    commands = verification.commands_for("ai-coding-agent:baseline-v1")
    assert commands[0] == (
        "ruff",
        "check",
        "agent.py",
        "ai_agent",
        "tests",
        "deploy/claude-code-statusline.py",
    )
    assert commands[-1] == ("python", "-m", "pytest", "-q")


def test_provider_schema_and_dashboard_outline_expose_explicit_checks():
    from pathlib import Path

    from ai_agent.plan_state import new_plan_state, plan_outline

    schema = json.loads(
        (Path(verification.__file__).parent / "schemas/feature_plan.json").read_text()
    )
    assert "verification_profile" in schema["required"]
    assert PROFILE in schema["properties"]["verification_profile"]["enum"]
    plan = new_plan_state(
        "Verify existing checks",
        json.dumps(
            {
                "verification_profile": PROFILE,
                "repositories": [{"name": "ai-dashboard", "changes": "verify"}],
                "repository_accounting": [{"name": "ai-dashboard", "role": "verify"}],
            }
        ),
        {"project_name": "ai-dashboard"},
    )
    steps = plan_outline(plan)["steps"]
    assert "no model or PR" in steps[0]
    assert "npm test" in steps
    plan.plan_text = json.dumps({"verification_profile": "unsupported"})
    assert "revise before approval" in plan_outline(plan)["steps"][0]
