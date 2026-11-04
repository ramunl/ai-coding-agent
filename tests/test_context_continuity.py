"""Deterministic CTX07 cross-task continuity: A's handoff grounds B's packet.

Task A and task B never share a repository history here: B's packet is built
from A's pinned archive record alone. These tests stay A->B framed and do not
repeat the selection/authorization coverage in test_context_packets.py or the
lifecycle effect coverage in test_context_lifecycle.py.
"""

import hashlib
import importlib.util
import json
import sys
import time
import types
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_agent import context_attempts as journal
from ai_agent import context_lifecycle as lifecycle
from ai_agent import context_recovery as recovery
from ai_agent.context_models import (
    AttemptSnapshot,
    Dependency,
    EvidenceReference,
    RecordReference,
    RepositoryIdentity,
    Scope,
)
from ai_agent.context_packets import build_packet, context_prompt
from ai_agent.context_sources import (
    ContextBlockedError,
    ContextRequest,
    RepositoryEvidence,
)
from ai_agent.context_store import ContextStore, record_names

# planning_provider imports the Anthropic SDK at module scope; the provider
# parity test below never calls it. Install the same minimal stand-in as
# test_context_snapshot.py so the test runs offline without the SDK.
try:
    _anthropic_available = importlib.util.find_spec("anthropic") is not None
except ValueError:
    _anthropic_available = True
if not _anthropic_available:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.Anthropic = lambda api_key, **kwargs: object()
    _anthropic.DefaultHttpxClient = lambda **kwargs: object()
    _anthropic.NotFoundError = type("NotFoundError", (Exception,), {})
    sys.modules["anthropic"] = _anthropic


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """One owner/repository archive shared by the journal and packet paths."""
    identity = RepositoryIdentity("repo", str(tmp_path / "repo"), "owner/repo")
    scope = Scope("owner", identity)
    store = ContextStore(scope, tmp_path / "archive")

    def factory(selected):
        return ContextStore(selected, tmp_path / "archive")

    monkeypatch.setattr(lifecycle, "ContextStore", factory)
    monkeypatch.setattr(recovery, "ContextStore", factory)
    monkeypatch.setattr(
        recovery,
        "_recovery_lease",
        lambda captured: nullcontext(Path(captured["repo_path"])),
    )
    return scope, store


@pytest.fixture
def scope(archive):
    return archive[0]


@pytest.fixture
def store(archive):
    return archive[1]


@pytest.fixture
def task_ids():
    return "task-a", "task-b"


def save_attempt(
    store,
    *,
    outcome="succeeded",
    summary="A pinned summary",
    evidence=(),
):
    """Write task A's succeeded (or terminal) handoff record."""
    record = AttemptSnapshot(
        record_id="attempt-a",
        scope=store.scope,
        summary=summary,
        task_id="task-a",
        attempt_id="attempt-a",
        outcome=outcome,
        evidence=evidence,
    )
    return store.write(record, expected_revision=0)


def ref_of(record):
    return RecordReference(record.kind, record.record_id, record.revision, record.scope)


def request_b(
    store,
    *,
    prerequisite=None,
    dependencies=(),
    documents=(),
    dependency_ids=(),
):
    """Compose B's explicit context request with A pinned as prerequisite."""
    return ContextRequest(
        scope=store.scope,
        authorized_owner_id=store.scope.owner_id,
        initiative_id="initiative-b",
        task_id="task-b",
        attempt_id="attempt-b",
        stores=(store,),
        prerequisites=(prerequisite,) if prerequisite is not None else (),
        dependencies=dependencies,
        dependency_ids=dependency_ids,
        documents=documents,
    )


def test_succeeded_handoff_grounds_b_without_rediscovery(store, monkeypatch):
    from ai_agent import context_index

    saved = save_attempt(store)
    ref = ref_of(saved)
    request = request_b(store, prerequisite=ref)

    monkeypatch.setattr(
        context_index, "load_index", Mock(side_effect=AssertionError("no rediscovery"))
    )
    monkeypatch.setattr(
        context_index,
        "rebuild_index",
        Mock(side_effect=AssertionError("no rediscovery")),
    )
    calls = []
    original = store.get

    def spy(kind, record_id, revision=None):
        calls.append((kind, record_id, revision))
        return original(kind, record_id, revision)

    monkeypatch.setattr(store, "get", spy)

    packet = build_packet(request)
    data = json.loads(packet.text)
    assert [record["ref"] for record in data["records"]] == [asdict(ref)]
    entry = data["records"][0]
    assert entry["summary"] == saved.summary
    assert entry["task_id"] == "task-a"
    assert entry["attempt_id"] == "attempt-a"
    assert entry["outcome"] == "succeeded"
    assert entry["sha256"] == hashlib.sha256(
        json.dumps(
            asdict(saved), sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert packet.reference.records == (ref,)
    # Source-grounded: only the pinned attempt revision and its freshness read.
    assert calls == [("attempt", "attempt-a", 1), ("attempt", "attempt-a", None)]


@pytest.mark.parametrize("outcome", ["failed", "blocked", "cancelled"])
def test_non_succeeded_prerequisite_blocks_without_completion_record(store, outcome):
    saved = save_attempt(store, outcome=outcome)
    request = request_b(store, prerequisite=ref_of(saved))
    with pytest.raises(ContextBlockedError, match="successful handoff"):
        build_packet(request)
    with store.directory() as directory:
        assert list(record_names(directory)) == ["attempt.attempt-a.1.json"]


def test_blocked_handoff_stops_b_attempt_before_record(archive):
    scope, store = archive
    saved = save_attempt(store, outcome="failed")
    spec = lifecycle.new_spec(scope.owner_id, asdict(scope.repository), "task-b")
    spec["prerequisites"] = [asdict(ref_of(saved))]
    with pytest.raises(ContextBlockedError, match="successful handoff"):
        journal.Attempt(spec, "feature/b", {})
    with store.directory() as directory:
        assert list(record_names(directory)) == ["attempt.attempt-a.1.json"]


def test_stale_repository_evidence_blocks_b(store):
    saved = save_attempt(store)
    root = Path(store.scope.repository.repo_path)
    root.mkdir(parents=True, exist_ok=True)
    text = "pinned repository evidence"
    (root / "contract.md").write_text(text)
    document = RepositoryEvidence(
        "contract.md", hashlib.sha256(text.encode()).hexdigest()
    )
    request = request_b(store, prerequisite=ref_of(saved), documents=(document,))
    packet = build_packet(request)
    assert (
        json.loads(packet.text)["repository_evidence"][0]["sha256"] == document.sha256
    )
    (root / "contract.md").write_text("drifted content")
    with pytest.raises(ContextBlockedError, match="Stale repository evidence"):
        build_packet(request)


def test_unmerged_reviewable_pr_dependency_blocks_b(store, monkeypatch):
    from ai_agent import task_pulls

    saved = save_attempt(
        store,
        evidence=(
            EvidenceReference(
                "pr-evidence",
                "pull_request",
                "https://github.com/owner/repo/pull/7",
                "opened PR",
            ),
            EvidenceReference("commit-evidence", "commit", "a" * 40, "head commit"),
        ),
    )
    dependency = Dependency(
        task_id="task-a",
        scope=store.scope,
        attempt=ref_of(saved),
        policy="reviewable_pr",
    )
    monkeypatch.setattr(
        task_pulls,
        "verified_pull",
        lambda repository, url, heads: {
            "reviewable": False,
            "merged": False,
            "checked_at": time.time(),
        },
    )
    request = request_b(store, prerequisite=ref_of(saved), dependencies=(dependency,))
    with pytest.raises(ContextBlockedError, match="Dependency evidence"):
        build_packet(request)


def test_cancelled_journal_fails_handoff(archive):
    scope, store = archive
    spec = lifecycle.new_spec(scope.owner_id, asdict(scope.repository), "task-a")
    attempt = journal.Attempt(spec, "feature/a", {})
    attempt.save()
    attempt.finish("cancelled")
    record = store.get("attempt", attempt.state["attempt_id"])
    assert record.outcome == "cancelled"
    request = request_b(store, prerequisite=ref_of(record))
    with pytest.raises(ContextBlockedError, match="successful handoff"):
        build_packet(request)


def test_reconcile_failed_ci_yields_failed_handoff(archive, monkeypatch):
    from ai_agent import ci, github, shell

    scope, store = archive
    spec = lifecycle.new_spec(scope.owner_id, asdict(scope.repository), "task-a")
    data = {
        "tasks": [
            {
                "id": "task-a",
                "repo": scope.repository.project_name,
                "branch": "feature/a",
            }
        ]
    }
    attempt = journal.Attempt(spec, "feature/a", data)
    attempt.save()
    attempt.intent("pr")
    monkeypatch.setattr(
        shell, "run", lambda args, **kwargs: SimpleNamespace(output="a" * 40)
    )
    monkeypatch.setattr(
        github,
        "github_request",
        lambda *args, **kwargs: [
            {
                "number": 7,
                "html_url": "https://example.test/pr/7",
                "head": {
                    "sha": "a" * 40,
                    "ref": "feature/a",
                    "repo": {"full_name": "owner/repo"},
                },
            }
        ],
    )
    monkeypatch.setattr(
        ci,
        "evaluate_ci",
        lambda sha, **kwargs: ci.CiResult(
            "failed", "CI failed", "https://example.test/ci"
        ),
    )
    assert recovery.reconcile(data, scope.owner_id)
    record = store.get("attempt", attempt.state["attempt_id"])
    assert record.outcome == "failed"


def test_retried_request_produces_identical_packet_bytes(store, monkeypatch):
    from ai_agent import context_packets

    saved = save_attempt(store)
    request = request_b(store, prerequisite=ref_of(saved))
    monkeypatch.setattr(context_packets.time, "time", lambda: 100)
    first = build_packet(request)
    monkeypatch.setattr(context_packets.time, "time", lambda: 200)
    second = build_packet(request)
    assert first.text == second.text
    assert first.reference == second.reference
    assert first.reference.sha256 == hashlib.sha256(first.text.encode()).hexdigest()


def test_provider_switch_receives_identical_b_packet(store, monkeypatch):
    from ai_agent import planning_provider as provider
    from ai_agent import projects, workflow

    saved = save_attempt(store)
    request = request_b(store, prerequisite=ref_of(saved))
    expected = context_prompt("TASK AND RULES", request)

    monkeypatch.setattr(provider, "diagnostic_context", lambda: "")
    codex = Mock(return_value="ok")
    claude = Mock(return_value="ok")
    monkeypatch.setattr(provider, "_codex_message", codex)
    monkeypatch.setattr(provider, "claude_code_message", claude)
    monkeypatch.setattr(provider, "CLAUDE_PLANNER_VIA", "code")
    provider.planner_message(
        "TASK AND RULES", "codex", "feature_plan.json", 1, context=request
    )
    provider.planner_message(
        "TASK AND RULES", "claude", "feature_plan.json", 1, context=request
    )
    assert codex.call_args.args[0] == expected
    assert claude.call_args.args[0] == expected

    identity = store.scope.repository
    monkeypatch.setattr(
        projects,
        "active_project",
        lambda: SimpleNamespace(
            name=identity.project_name,
            repo_path=identity.repo_path,
            github_repository=identity.github_repository,
        ),
    )
    monkeypatch.setattr(workflow, "diagnostic_context", lambda: "")
    monkeypatch.setattr(workflow, "OUTCOME_INSTRUCTIONS", "")
    runner = Mock(return_value=SimpleNamespace(output="ok"))
    monkeypatch.setattr(workflow, "run_with_images", runner)
    prepared = context_prompt("TASK", request)
    for agent in ("codex", "claude"):
        workflow.run_implementation_agent("TASK", agent, context=request)
    commands = [call.args[0] for call in runner.call_args_list]
    assert prepared in commands[0]
    assert prepared in commands[1]
