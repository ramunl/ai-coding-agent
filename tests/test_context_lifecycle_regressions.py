"""Regressions at provider, retry, recovery and legacy task boundaries."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_agent import context_attempts as journal
from ai_agent import context_lifecycle as lifecycle
from ai_agent import context_recovery as recovery
from tests.test_context_lifecycle import bot_modules as bot_modules
from tests.test_context_lifecycle import setup as setup


def test_resume_composes_context_after_restoring_saved_checkout(setup, monkeypatch):
    from ai_agent import checkpoints, workflow
    from ai_agent.git_recovery import RecoverySession

    spec, _ = setup
    request = lifecycle.request_from_spec(spec)
    events = []
    monkeypatch.setattr(
        workflow,
        "_session",
        SimpleNamespace(get=lambda: RecoverySession(task_id="task", resume=True)),
    )

    def restore(*args):
        events.append("restore")
        return {"head": "b" * 40}

    monkeypatch.setattr(checkpoints, "restore", restore)
    monkeypatch.setattr(
        workflow, "active_project", lambda: SimpleNamespace(base_branch="main")
    )

    def command(args):
        events.append(args)
        return SimpleNamespace(
            output="b" * 40 if args == ["git", "rev-parse", "HEAD"] else "diff"
        )

    monkeypatch.setattr(workflow, "run", command)
    monkeypatch.setattr(workflow, "changed_files", lambda: ["saved.py"])
    seen = []

    def agent(prompt, provider, *, context):
        seen.append(context)
        assert "CONTINUE SAVED WORK" in prompt
        assert events[0] == "restore"
        return SimpleNamespace(output="done")

    monkeypatch.setattr(workflow, "run_implementation_agent", agent)
    workflow.resume_implementation.__wrapped__.__wrapped__(
        "approved", "feature/task", "codex", context=request
    )
    assert seen == [replace(request, repository_head="b" * 40)]


def test_recovery_does_not_attach_pr_to_another_repo_or_task(setup, monkeypatch):
    from ai_agent import ci, github, plan_state, shell

    spec, plan = setup
    tasks = [
        {"id": "task", "repo": "other", "branch": "feature/task"},
        {"id": "other", "repo": "project", "branch": "feature/task"},
        {"id": "task", "repo": "project", "branch": "feature/task"},
    ]
    data = {"tasks": tasks}
    attempt = journal.Attempt(spec, "feature/task", data)
    attempt.save()
    attempt.intent("pr")
    monkeypatch.setattr(
        plan_state, "capture_repository_identity", lambda: plan.repository_identity
    )
    monkeypatch.setattr(
        shell, "run", lambda args, **kwargs: SimpleNamespace(output="a" * 40)
    )
    monkeypatch.setattr(
        github,
        "github_request",
        lambda *a, **k: [
            {
                "number": 1,
                "html_url": "https://example.test/pr/1",
                "head": {
                    "sha": "a" * 40,
                    "ref": "feature/task",
                    "repo": {"full_name": "owner/project"},
                },
            }
        ],
    )
    monkeypatch.setattr(
        ci, "evaluate_ci", lambda sha, **kwargs: ci.CiResult("passed", "ok", None)
    )
    assert recovery.reconcile(data, 123)
    assert "pr_url" not in tasks[0]
    assert "pr_url" not in tasks[1]
    assert tasks[2]["pr_url"] == "https://example.test/pr/1"
    assert "context_handoff" not in tasks[0]
    assert "context_handoff" not in tasks[1]
    assert tasks[2]["context_handoff"]


def test_archived_handoff_includes_observed_partial_work_facts(setup):
    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/task", {})
    attempt.save()
    attempt.checkpoint("checkout", {"sha": "a" * 40})
    attempt.checkpoint(
        "diff",
        {"sha256": "b" * 64, "files": ["src/partial.py"], "model_tests": "unverified"},
    )
    attempt.finish("failed")
    record = attempt.store.get("attempt", attempt.state["attempt_id"])
    import json
    from dataclasses import asdict

    archived = json.dumps(asdict(record))
    assert "src/partial.py" in archived
    assert "a" * 40 in archived
    assert "b" * 64 in archived
    assert "unverified" in archived


def test_legacy_completed_dependency_without_handoff_remains_plannable(setup):
    from ai_agent.tasks import waiting_for

    _, plan = setup
    data = {
        "planning_task": "new",
        "tasks": [{"id": "old", "stage": "done"}, {"id": "new", "after": "old"}],
    }
    assert waiting_for(data["tasks"][1], data) is None
    request = lifecycle.planning_request(data, "123", plan.repository_identity)
    assert not request.dependency_ids
    assert lifecycle.build_packet(request)


def test_handled_failure_before_publication_allows_next_attempt(setup, bot_modules):
    from ai_agent.bot.context_execution import lifecycle_execution

    spec, _ = setup
    data = {}
    item = {"context_spec": spec, "branch_name": "feature/task"}

    @lifecycle_execution
    async def failed(update, context, item):
        journal.checkpoint("provider", status="started")
        raise RuntimeError("simulated provider failure")

    with pytest.raises(RuntimeError):
        asyncio.run(failed(None, SimpleNamespace(user_data=data), item))
    assert not recovery.detect_pending(123)

    @lifecycle_execution
    async def next_attempt(update, context, item):
        journal.checkpoint("no_code", outcome="blocked")

    asyncio.run(next_attempt(None, SimpleNamespace(user_data=data), item))
    assert not recovery.detect_pending(123)


def test_execution_rejects_context_owned_by_another_user(setup, bot_modules):
    from ai_agent.bot.context_execution import lifecycle_execution
    from ai_agent.context_sources import ContextBlockedError

    spec, _ = setup
    foreign = {**spec, "scope": {**spec["scope"], "owner_id": "456"}}
    calls = []

    @lifecycle_execution
    async def execute(update, context, item):
        calls.append(True)

    with pytest.raises(ContextBlockedError):
        asyncio.run(
            execute(
                None,
                SimpleNamespace(user_data={}),
                {"context_spec": foreign, "branch_name": "feature/task"},
            )
        )
    assert not calls


def test_direct_bugfix_seals_sources_until_confirmation(
    setup, bot_modules, monkeypatch
):
    from ai_agent.bot import planning
    from ai_agent.context_sources import ContextBlockedError

    _, plan = setup
    monkeypatch.setattr(
        planning, "capture_repository_identity", lambda: plan.repository_identity
    )
    monkeypatch.setattr(planning, "build_bugfix_prompt", lambda bug: "approved fix")

    async def reply(*args):
        pass

    monkeypatch.setattr(planning, "reply_chunks", reply)
    data = {}
    asyncio.run(
        planning.prepare_bugfix(
            None, SimpleNamespace(user_data=data), "broken", "broken"
        )
    )
    item = data["pending_implementation"]
    assert item["approval_binding"]["sources"]
    lifecycle.require_run(item)
    original = lifecycle.build_packet(lifecycle.request_from_spec(item["context_spec"]))
    monkeypatch.setattr(
        lifecycle,
        "build_packet",
        lambda request: replace(
            original, reference=replace(original.reference, sha256="f" * 64)
        ),
    )
    with pytest.raises(ContextBlockedError, match="context changed"):
        lifecycle.require_run(item)


@pytest.mark.parametrize("publication_intent", [False, True])
def test_crash_recovery_distinguishes_unpublished_from_unknown_pr_effect(
    setup, monkeypatch, publication_intent
):
    from ai_agent import github, shell

    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/task", {})
    attempt.save()
    attempt.intent("pr" if publication_intent else "implementation")
    monkeypatch.setattr(
        shell, "run", lambda args, **kwargs: SimpleNamespace(output="a" * 40)
    )
    monkeypatch.setattr(github, "github_request", lambda *args, **kwargs: [])
    data = {}
    assert recovery.reconcile(data, 123) is not publication_intent
    assert recovery.detect_pending(123) is publication_intent
    if not publication_intent:
        assert (
            attempt.store.get("attempt", attempt.state["attempt_id"]).outcome
            == "failed"
        )


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_interrupted_provider_archives_application_observed_partial_work(
    setup, monkeypatch, provider, error_type
):
    import hashlib
    import json
    from dataclasses import asdict

    from ai_agent import workflow

    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/task", {})
    attempt.save()
    request = lifecycle.request_from_spec(spec)
    partial_diff = "diff --git a/src/partial.py b/src/partial.py\n+unfinished change\n"

    def command(args):
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(output="a" * 40)
        if args == ["git", "diff", "--no-ext-diff", "HEAD"]:
            return SimpleNamespace(output=partial_diff)
        return SimpleNamespace(output="")

    def fail(prompt, agent, *, context):
        assert agent == provider
        raise error_type("provider ended after writing partial.py")

    monkeypatch.setattr(workflow, "run", command)
    monkeypatch.setattr(workflow, "changed_files", lambda: ["src/partial.py"])
    monkeypatch.setattr(workflow, "run_implementation_agent", fail)
    token = journal.current_attempt.set(attempt)
    try:
        with pytest.raises(error_type):
            workflow._run_and_capture_changes("approved", provider, request)
        attempt.finish("failed")
    finally:
        journal.current_attempt.reset(token)
    archived = json.dumps(
        asdict(attempt.store.get("attempt", attempt.state["attempt_id"]))
    )
    assert "src/partial.py" in archived
    assert "a" * 40 in archived
    assert hashlib.sha256(partial_diff.encode()).hexdigest() in archived
    assert partial_diff not in archived


def test_long_approved_prompt_survives_retry_persistence_exactly(setup):
    from ai_agent.bot.persistence import deserialize_user_data, serialize_user_data
    from ai_agent.tasks import remember_run

    spec, plan = setup
    prompt = "approved details " * 2000
    item = {
        "context_spec": spec,
        "repository_identity": plan.repository_identity,
        "branch_name": "feature/task",
        "codex_prompt": prompt,
    }
    item["run_binding"] = lifecycle.run_binding(item)
    data = {
        "tasks": [
            {
                "id": "task",
                "repo": "project",
                "branch": "feature/task",
                "stage": "planned",
            }
        ]
    }
    remember_run(data, item)
    restored = deserialize_user_data(serialize_user_data(data))
    run = restored["tasks"][0]["run"]
    assert len(run["codex_prompt"]) > 20000
    assert run["codex_prompt"] == prompt
    assert run["run_binding"] == item["run_binding"]
    lifecycle.require_run(run)


def test_oversized_sealed_prompt_is_refused_without_silent_truncation(setup):
    from ai_agent.context_sources import ContextBlockedError

    spec, _ = setup
    item = {"context_spec": spec, "codex_prompt": "a" * 256001}
    item["run_binding"] = lifecycle.run_binding(item)
    with pytest.raises(ContextBlockedError):
        lifecycle.require_run(item)
    assert len(item["codex_prompt"]) == 256001
    assert item["run_binding"] == lifecycle.run_binding(item)


def test_precheckout_crash_without_effect_intent_closes_without_git_or_github(
    setup, monkeypatch
):
    from ai_agent import github, shell

    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/not-created-yet", {})
    attempt.save()

    def forbidden(*args, **kwargs):
        pytest.fail("An empty preexecution journal needs no branch or remote evidence")

    monkeypatch.setattr(shell, "run", forbidden)
    monkeypatch.setattr(github, "github_request", forbidden)
    data = {}
    assert recovery.reconcile(data, 123)
    assert not recovery.detect_pending(123)
    assert attempt.store.get("attempt", attempt.state["attempt_id"]).outcome == "failed"
