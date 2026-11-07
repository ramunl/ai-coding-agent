"""Approval, effect gaps and immutable lifecycle handoffs."""

import asyncio
import json
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent import context_attempts as journal
from ai_agent import context_lifecycle as lifecycle
from ai_agent import context_recovery as recovery
from ai_agent import task_progress
from ai_agent.context_models import Decision, RecordReference, RepositoryContext
from ai_agent.context_sources import ContextBlockedError
from ai_agent.context_store import ContextPersistenceError, ContextStore
from ai_agent.plan_state import new_plan_state, revise_plan_state


@pytest.fixture
def setup(tmp_path, monkeypatch):
    factory = lambda scope: ContextStore(scope, tmp_path / "archive")
    monkeypatch.setattr(lifecycle, "ContextStore", factory)
    monkeypatch.setattr(recovery, "ContextStore", factory)
    monkeypatch.setattr(
        recovery,
        "_recovery_lease",
        lambda identity: nullcontext(Path(identity["repo_path"])),
    )
    identity = {
        "project_name": "project",
        "repo_path": str(tmp_path / "repo"),
        "github_repository": "owner/project",
    }
    spec = lifecycle.new_spec("123", identity, "task")
    plan = new_plan_state("feature", "exact plan", identity)
    plan.context_spec = spec
    return spec, plan


@pytest.fixture
def bot_modules(setup):
    from tests.bot_fixtures import TelegramTestCase

    case = TelegramTestCase()
    case.setUp()
    try:
        yield
    finally:
        case.tearDown()
        case.doCleanups()


def test_approval_survives_json_but_not_material_changes(setup):
    _, plan = setup
    lifecycle.approve_plan(plan)
    plan.approval_binding = json.loads(json.dumps(plan.approval_binding))
    lifecycle.require_plan_approval(plan)
    plan.plan_text += " more work"
    with pytest.raises(ContextBlockedError, match="review"):
        lifecycle.require_plan_approval(plan)
    assert not plan.approved
    revised = revise_plan_state(plan, "new plan")
    assert not revised.approved and revised.approval_binding is None
    assert revised.context_spec == plan.context_spec


def test_stale_decision_revision_invalidates_approval(setup):
    spec, plan = setup
    request = lifecycle.request_from_spec(spec)
    store = request.stores[0]
    record = store.write(
        RepositoryContext(
            record_id="rules",
            scope=request.scope,
            summary="constraints",
            decisions=(Decision("choice", "proposed rule"),),
        ),
        expected_revision=0,
    )
    ref = RecordReference("repository", "rules", 1, request.scope)
    spec["constraints"] = [asdict(ref)]
    lifecycle.approve_plan(plan)
    store.write(replace(record, summary="material revision"), expected_revision=1)
    with pytest.raises(ContextBlockedError, match="Stale"):
        lifecycle.require_plan_approval(plan)
    assert not plan.approved


def test_run_seal_allows_provider_switch_only(setup):
    spec, _ = setup
    item = {
        "context_spec": spec,
        "codex_prompt": "approved",
        "branch_name": "feature/a",
    }
    item["run_binding"] = lifecycle.run_binding(item)
    item["implementation_agent"] = "claude"
    lifecycle.require_run(item)
    item["codex_prompt"] = "different"
    with pytest.raises(ContextBlockedError):
        lifecycle.require_run(item)
    lifecycle.require_run({"codex_prompt": "legacy"})


def test_duplicate_checkpoints_and_terminal_handoffs_are_immutable(setup):
    spec, _ = setup
    attempt = journal.Attempt(
        spec,
        "feature/a",
        {"tasks": [{"id": "task", "repo": "project", "branch": "feature/a"}]},
    )
    attempt.save()
    attempt.checkpoint("commit", {"sha": "a" * 40})
    attempt.checkpoint("commit", {"sha": "a" * 40})
    assert len(attempt.state["events"]) == 1
    attempt.finish("succeeded")
    assert not attempt.failed
    attempt.finish("succeeded")
    with pytest.raises(ContextBlockedError):
        attempt.finish("failed")
    with pytest.raises(ContextBlockedError):
        attempt.checkpoint("ci", {"state": "failed"})
    record = attempt.store.get("attempt", attempt.state["attempt_id"])
    assert record.outcome == "succeeded"
    assert "unverified" in record.summary
    assert attempt.data["tasks"][0]["context_handoff"]["record_id"] == record.record_id
    assert journal.unfinished(attempt.store) == []


def test_write_failure_retains_pr_and_blocks_next_effect(setup, monkeypatch):
    spec, _ = setup
    data = {}
    attempt = journal.Attempt(spec, "feature/a", data)
    attempt.save()
    attempt.intent("pr")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(journal, "atomic_write", fail)
    attempt.checkpoint("pr", {"url": "https://example.test/pr/1", "sha": "a" * 40})
    assert data["context_recovery_pending"]
    assert any(e["stage"] == "pr" for e in attempt.state["events"].values())
    with pytest.raises(ContextBlockedError):
        attempt.intent("push")
    assert recovery.detect_pending(123)


def test_handoff_fsync_gap_is_idempotent(setup, monkeypatch):
    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    original = attempt.store.write

    def fail_after_commit(record, **kwargs):
        original(record, **kwargs)
        raise ContextPersistenceError("directory fsync failed")

    monkeypatch.setattr(attempt.store, "write", fail_after_commit)
    attempt.finish("succeeded")
    assert not attempt.failed
    assert not recovery.detect_pending(123)


def test_terminal_draft_recovers_without_repeating_effects(setup, monkeypatch):
    spec, _ = setup
    data = {}
    attempt = journal.Attempt(spec, "feature/a", data)
    attempt.save()
    with monkeypatch.context() as local:

        def fail(*args, **kwargs):
            raise ContextPersistenceError("disk full")

        local.setattr(attempt.store, "write", fail)
        attempt.finish("succeeded")
    assert recovery.detect_pending(123)
    assert recovery.reconcile(data, 123)
    assert (
        attempt.store.get("attempt", attempt.state["attempt_id"]).outcome == "succeeded"
    )


def test_restart_without_remote_evidence_never_repeats_effect(setup, monkeypatch):
    from ai_agent import github, plan_state, shell

    spec, plan = setup
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    attempt.intent("pr")
    monkeypatch.setattr(
        plan_state, "capture_repository_identity", lambda: plan.repository_identity
    )
    monkeypatch.setattr(
        shell, "run", lambda args, **kwargs: SimpleNamespace(output="a" * 40)
    )
    calls = []

    def missing(method, path, **kwargs):
        calls.append(method)
        return []

    monkeypatch.setattr(github, "github_request", missing)
    data = {}
    assert not recovery.reconcile(data, 123)
    assert calls == ["GET"]
    assert data["context_recovery_pending"]


def test_restart_recovers_pr_and_ci_with_provenance(setup, monkeypatch):
    from ai_agent import ci, github, plan_state, shell

    spec, plan = setup
    data = {"tasks": [{"id": "task", "repo": "project", "branch": "feature/a"}]}
    attempt = journal.Attempt(spec, "feature/a", data)
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
                "number": 7,
                "html_url": "https://example.test/pr/7",
                "head": {
                    "sha": "a" * 40,
                    "ref": "feature/a",
                    "repo": {"full_name": "owner/project"},
                },
            }
        ],
    )
    monkeypatch.setattr(
        ci,
        "evaluate_ci",
        lambda sha, **kwargs: ci.CiResult("passed", "ok", "https://example.test/ci"),
    )
    assert recovery.reconcile(data, 123)
    assert data["tasks"][0]["pr_url"] == "https://example.test/pr/7"
    record = attempt.store.get("attempt", attempt.state["attempt_id"])
    assert {e.source for e in record.evidence} == {"pull_request", "test"}


def test_recovery_blocks_retry_reopen_and_dependencies():
    from ai_agent.task_reopen import reopen_refusal
    from ai_agent.tasks import waiting_for

    data = {"context_recovery_pending": True}
    assert lifecycle.recovery_refusal(data)
    assert reopen_refusal({"stage": "done"}, data)
    assert waiting_for({"after": "previous"}, data)


def test_cancelled_outer_waits_for_actual_completion(setup):
    from ai_agent.git_recovery import serialized_execution

    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/a", {})

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        @serialized_execution
        async def run():
            attempt.save()
            started.set()
            await release.wait()
            attempt.finish("succeeded")

        task = asyncio.create_task(run())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert recovery.detect_pending(123)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not recovery.detect_pending(123)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation", ["implement", "repair_implementation", "repair_pull_request_branch"]
)
def test_execution_packets_use_head_after_checkout(setup, monkeypatch, operation):
    from ai_agent import workflow

    spec, _ = setup
    request = lifecycle.request_from_spec(spec)
    calls, packets = [], []
    monkeypatch.setattr(
        workflow, "active_project", lambda: SimpleNamespace(base_branch="main")
    )
    monkeypatch.setattr(workflow, "retain_branch", lambda branch: None)

    def command(args):
        calls.append(args)
        return SimpleNamespace(
            output="a" * 40 if args == ["git", "rev-parse", "HEAD"] else "diff"
        )

    monkeypatch.setattr(workflow, "run", command)
    monkeypatch.setattr(workflow, "changed_files", lambda: ["file.py"])

    def agent(prompt, provider, *, context):
        packets.append(json.loads(lifecycle.build_packet(context).text))
        assert calls[-1] == ["git", "rev-parse", "HEAD"]
        assert any(command[1] == "checkout" for command in calls[:-1])
        return SimpleNamespace(output="Tests passed (model claim)")

    monkeypatch.setattr(workflow, "run_implementation_agent", agent)
    # Exercise the operation without the separately tested checkout lease.
    function = getattr(workflow, operation).__wrapped__.__wrapped__
    function("original prompt", "feature/a", "codex", context=request)
    assert packets[0]["repository_head"] == "a" * 40


def test_existing_pr_is_reused_without_post(setup, monkeypatch):
    from ai_agent import workflow

    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    monkeypatch.setattr(workflow, "ensure_github_configured", lambda: None)
    monkeypatch.setattr(
        workflow,
        "active_project",
        lambda: SimpleNamespace(base_branch="main", github_repository="owner/project"),
    )
    monkeypatch.setattr(
        workflow, "run", lambda args, **kwargs: SimpleNamespace(output="a" * 40)
    )
    calls = []

    def github(method, path, **kwargs):
        calls.append(method)
        return [
            {
                "number": 1,
                "html_url": "https://example.test/pr/1",
                "head": {"sha": "a" * 40},
            }
        ]

    monkeypatch.setattr(workflow, "github_request", github)
    token = journal.current_attempt.set(attempt)
    try:
        result = workflow.create_pull_request("feature/a", "change", "body")
    finally:
        journal.current_attempt.reset(token)
    assert result.number == 1 and calls == ["GET"]


def test_execution_boundary_retains_success_after_repeated_cancellation(
    setup, bot_modules
):
    from ai_agent.bot.context_execution import lifecycle_execution
    from ai_agent.git_recovery import serialized_execution

    spec, _ = setup
    data = {}
    item = {"context_spec": spec, "branch_name": "feature/a"}

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        @serialized_execution
        @lifecycle_execution
        async def execute(update, context, item):
            started.set()
            await release.wait()
            journal.checkpoint("ci", state="passed", sha="a" * 40)

        task = asyncio.create_task(execute(None, SimpleNamespace(user_data=data), item))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        pending = journal.unfinished(lifecycle.request_from_spec(spec).stores[0])
        assert any(
            e["stage"] == "cancellation_requested"
            for e in pending[0]["events"].values()
        )
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not recovery.detect_pending(123)

    asyncio.run(scenario())


def test_mutated_queue_cannot_reach_any_execution_provider(setup, bot_modules):
    from ai_agent.bot.context_execution import lifecycle_execution

    spec, _ = setup
    item = {
        "context_spec": spec,
        "codex_prompt": "approved",
        "branch_name": "feature/a",
    }
    item["run_binding"] = lifecycle.run_binding(item)
    item["codex_prompt"] = "unapproved"
    called = []

    @lifecycle_execution
    async def execute(update, context, item):
        called.append(True)

    with pytest.raises(ContextBlockedError):
        asyncio.run(execute(None, SimpleNamespace(user_data={}), item))
    assert not called


def test_repeated_ci_observation_updates_latest_without_mutating_checkpoint(setup):
    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    attempt.checkpoint("ci", {"state": "failed"})
    first = attempt.state["latest"]["ci"]
    attempt.checkpoint("ci", {"state": "passed"})
    attempt.checkpoint("ci", {"state": "failed"})
    assert len(attempt.state["events"]) == 2
    assert attempt.state["latest"]["ci"] == first


def test_planning_request_keeps_explicit_constraints_and_reopen_handoffs(setup):
    spec, plan = setup
    first = journal.Attempt(spec, "feature/first", {})
    first.save()
    first.finish("failed")
    ref = {
        "kind": "attempt",
        "record_id": first.state["attempt_id"],
        "revision": 1,
        "scope": spec["scope"],
    }
    data = {
        "planning_task": "task",
        "tasks": [{"id": "task", "context_handoffs": [ref]}],
    }
    request = lifecycle.planning_request(data, "123", plan.repository_identity)
    packet = json.loads(lifecycle.build_packet(request).text)
    assert packet["records"][0]["outcome"] == "failed"
    assert packet["records"][0]["task_id"] == "task"


def test_plan_revision_keeps_task_identity_outside_initial_planning_handler(
    setup, bot_modules
):
    from ai_agent.bot.state import planning_guard

    spec, plan = setup
    with planning_guard({}, identity=plan.repository_identity, context_spec=spec):
        revised = revise_plan_state(plan, "revised")
    assert revised.context_spec["task_id"] == "task"
    assert revised.context_spec["scope"] == spec["scope"]
    assert lifecycle.current_context.get() is None


def test_retry_rechecks_packet_digest_even_without_pending_plan(setup, monkeypatch):
    spec, plan = setup
    lifecycle.approve_plan(plan)
    item = {"context_spec": spec, "approval_binding": plan.approval_binding}
    item["run_binding"] = lifecycle.run_binding(item)
    original = lifecycle.build_packet(lifecycle.request_from_spec(spec))
    monkeypatch.setattr(
        lifecycle,
        "build_packet",
        lambda request, **kwargs: replace(
            original, reference=replace(original.reference, sha256="f" * 64)
        ),
    )
    with pytest.raises(ContextBlockedError, match="context changed"):
        lifecycle.require_run(item)


def test_owner_scope_packet_keeps_full_journal_and_scoped_terminal_refs(setup):
    from ai_agent.context_models import OwnerContext, Scope

    spec, _ = setup
    scope = Scope("123")
    store = lifecycle.ContextStore(scope)
    store.write(
        OwnerContext(record_id="preferences", scope=scope, summary="owner preference"),
        expected_revision=0,
    )
    spec["constraints"] = [asdict(RecordReference("owner", "preferences", 1, scope))]
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    packet = lifecycle.build_packet(attempt.request)
    attempt.checkpoint(
        "checkout", {"sha": "a" * 40, "packet": asdict(packet.reference)}
    )
    attempt.finish("succeeded")
    assert not attempt.failed
    saved = attempt.store.get("attempt", attempt.state["attempt_id"])
    assert saved.packets[0].sha256 == packet.reference.sha256
    assert saved.packets[0].records == ()
    event = next(iter(attempt.state["events"].values()))
    assert event["facts"]["packet"]["records"][0]["scope"]["repository"] is None


def test_receipt_metadata_is_additive_and_does_not_change_approval(setup, monkeypatch):
    from ai_agent import context_packets
    from ai_agent.context_status import PacketMetadata

    spec, plan = setup
    receipt = {}
    token = lifecycle.packet_receipt.set(receipt)
    try:
        monkeypatch.setattr(context_packets.time, "time", lambda: 100)
        context_packets.context_prompt(
            "private prompt", lifecycle.request_from_spec(spec)
        )
        first = dict(receipt)
        plan.context_packet = receipt["packet"]
        lifecycle.approve_plan(plan)
        binding = lifecycle.plan_binding(plan)
        monkeypatch.setattr(context_packets.time, "time", lambda: 200)
        context_packets.context_prompt(
            "private prompt", lifecycle.request_from_spec(spec)
        )
        assert receipt["packet"] == first["packet"]
        assert receipt["packet_metadata"]["observed_at"] == 200
        assert first["packet_metadata"]["observed_at"] == 100
        assert lifecycle.plan_binding(plan) == binding
        lifecycle.require_plan_approval(plan)
        assert "packet_metadata" not in json.dumps(binding)
        assert "private prompt" not in json.dumps(receipt)
        assert (
            PacketMetadata.from_dict({"packet": first["packet"]}.get("packet_metadata"))
            == PacketMetadata()
        )
        item = {"context_spec": spec, "approval_binding": binding}
        before = lifecycle.run_binding(item)
        item["packet_metadata"] = receipt["packet_metadata"]
        assert lifecycle.run_binding(item) == before
    finally:
        lifecycle.packet_receipt.reset(token)


def test_receipt_and_journal_expose_packet_metrics_without_secrets(setup, monkeypatch):
    from ai_agent import context_packets

    spec, _ = setup
    receipt = {}
    token = lifecycle.packet_receipt.set(receipt)
    try:
        monkeypatch.setattr(context_packets.time, "time", lambda: 100)
        packet = lifecycle.build_packet(lifecycle.request_from_spec(spec))
        context_packets.context_prompt(
            "prompt with token=journal-secret", lifecycle.request_from_spec(spec)
        )
        meta = receipt["packet_metadata"]
        assert meta["byte_size"] == len(packet.text.encode())
        assert type(meta["build_latency_ms"]) is int
        assert meta["build_latency_ms"] >= 0
        assert set(meta) == {
            "schema_version",
            "observed_at",
            "verified",
            "omission_count",
            "truncation_count",
            "byte_size",
            "build_latency_ms",
        }
        assert "journal-secret" not in json.dumps(receipt)
    finally:
        lifecycle.packet_receipt.reset(token)

    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    attempt.checkpoint(
        "checkout",
        {
            "sha": "a" * 40,
            "packet": asdict(packet.reference),
            "packet_metadata": packet.metadata.to_dict(),
            "note": "token=journal-secret",
        },
    )
    event = next(iter(attempt.state["events"].values()))
    facts = event["facts"]
    assert facts["packet_metadata"]["byte_size"] == len(packet.text.encode())
    assert facts["packet_metadata"]["build_latency_ms"] >= 0
    # Redaction strips the seeded secret while leaving integer metrics intact.
    assert "journal-secret" not in json.dumps(attempt.state)
    projected = packet.metadata.to_dict()
    assert facts["packet_metadata"]["byte_size"] == projected["byte_size"]


def test_journal_timestamps_are_recorded_and_backward_compatible(setup, monkeypatch):
    spec, _ = setup
    monkeypatch.setattr(journal.time, "time", lambda: 100.0)
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    assert attempt.state["started_at"] == 100.0
    monkeypatch.setattr(journal.time, "time", lambda: 150.0)
    attempt.checkpoint("checkout", {"sha": "a" * 40})
    event = next(iter(attempt.state["events"].values()))
    assert event["observed_at"] == 150.0

    view = task_progress.journal_progress(attempt.state, now=200.0)
    assert view == {
        "task_id": "task",
        "attempt_id": attempt.state["attempt_id"],
        "stage": "unknown",
        "elapsed": 100,
        "last_activity": 50,
    }

    # A legacy journal (events without observed_at) still projects conservatively.
    legacy = {
        "spec": {"task_id": "task"},
        "attempt_id": "legacy",
        "started_at": 100.0,
        "events": {"k": {"stage": "ci", "facts": {}}},
        "latest": {"ci": "k"},
        "terminal": None,
    }
    assert task_progress.journal_progress(legacy, now=200.0)["last_activity"] is None


def test_terminal_attempt_has_no_live_progress(setup):
    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    attempt.checkpoint("ci", {"state": "passed", "sha": "a" * 40})
    attempt.finish("succeeded")
    assert task_progress.journal_progress(attempt.state) is None


def test_stale_attempt_never_masks_a_new_attempt(setup):
    spec, _ = setup
    first = journal.Attempt(spec, "feature/a", {})
    first.save()
    first.finish("failed")

    second = journal.Attempt(spec, "feature/b", {})
    second.save()
    second.checkpoint("checkout", {"sha": "b" * 40})

    assert task_progress.journal_progress(first.state) is None
    live = task_progress.journal_progress(second.state)
    assert live is not None
    assert live["attempt_id"] == second.state["attempt_id"]
    assert live["attempt_id"] != first.state["attempt_id"]


def test_progress_projection_is_bounded_and_secret_free(setup):
    spec, _ = setup
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    attempt.checkpoint(
        "diff",
        {
            "files": ["/secret/path/file.py"],
            "sha256": "x" * 64,
            "note": "token=secret prompt",
        },
    )
    view = task_progress.journal_progress(attempt.state)
    serialized = json.dumps(view, sort_keys=True)
    assert "secret" not in serialized
    assert "/secret" not in serialized
    assert set(view) == {"task_id", "attempt_id", "stage", "elapsed", "last_activity"}
    assert all(
        value is None or isinstance(value, (str, int)) for value in view.values()
    )
