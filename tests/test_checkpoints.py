"""Real Git recovery preserves task progress and staged/untracked edits."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from ai_agent import checkpoints, git_recovery, provider_process, shell, workflow
from ai_agent.shell import CommandResult


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "tracked").write_text("initial\n")
    git("add", ".")
    git("commit", "-m", "initial")
    git("remote", "add", "origin", str(tmp_path))
    project = SimpleNamespace(repo_path=tmp_path, base_branch="main")
    for module in (checkpoints, git_recovery, shell, workflow, provider_process):
        monkeypatch.setattr(module, "active_project", lambda: project)
    return tmp_path, git


def _fail(repo, monkeypatch, *, committed=False):
    path, git = repo

    def provider(*args):
        if committed:
            (path / "committed").write_text("partial commit\n")
            git("add", "committed")
            git("commit", "-m", "partial")
        (path / "tracked").write_text("staged\n")
        git("add", "tracked")
        (path / "tracked").write_text("unstaged\n")
        (path / "untracked").write_text("untracked\n")
        raise RuntimeError("interrupted")

    monkeypatch.setattr(workflow, "run_implementation_agent", provider)
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume")
        with pytest.raises(RuntimeError, match="interrupted"):
            workflow.implement("original approved plan", "feature/resume")
        git_recovery.cleanup_recovery()
    git("checkout", "main")


@pytest.mark.parametrize("committed", [True, False])
def test_resume_restores_commits_index_worktree_and_untracked(
    repo, monkeypatch, committed
):
    path, git = repo
    _fail(repo, monkeypatch, committed=committed)
    assert checkpoints.status("aabbccdd", "feature/resume")["can_resume"]
    seen = {}

    def provider(prompt, *_):
        seen["prompt"] = prompt
        assert git("show", ":tracked") == "staged"
        assert (path / "tracked").read_text() == "unstaged\n"
        assert (path / "untracked").read_text() == "untracked\n"
        assert (path / "committed").exists() == committed
        return CommandResult([], 0, "finished")

    monkeypatch.setattr(workflow, "run_implementation_agent", provider)
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume", True)
        workflow.resume_implementation("original approved plan", "feature/resume")
        git_recovery.cleanup_recovery()
    assert "original approved plan" in seen["prompt"]
    assert "fresh model invocation" in seen["prompt"]
    # Repeated interruption retains exactly the same index and worktree.
    git("checkout", "main")
    assert checkpoints.status("aabbccdd", "feature/resume")["can_resume"]


def test_start_fresh_keeps_old_progress_but_starts_base(repo, monkeypatch):
    path, git = repo
    _fail(repo, monkeypatch, committed=True)
    old = checkpoints.load("aabbccdd", "feature/resume")

    def provider(*_):
        assert not (path / "committed").exists()
        assert not (path / "untracked").exists()
        assert (path / "tracked").read_text() == "initial\n"
        (path / "new").write_text("fresh")
        return CommandResult([], 0, "done")

    monkeypatch.setattr(workflow, "run_implementation_agent", provider)
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume")
        workflow.implement("approved", "feature/resume")
    assert git("show", old["refs"][0] + ":committed") == "partial commit"
    assert git("show", old["refs"][1] + ":tracked") == "unstaged"


def test_restart_recovers_only_original_bound_checkout(repo, monkeypatch):
    path, git = repo
    git("checkout", "-b", "feature/resume")
    checkpoints.begin("aabbccdd", "feature/resume")
    (path / "tracked").write_text("restart work\n")
    (path / "new").write_text("new\n")
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume", True)
        monkeypatch.setattr(
            workflow,
            "run_implementation_agent",
            lambda *_: CommandResult([], 0, "done"),
        )
        workflow.resume_implementation("approved", "feature/resume")
    assert (path / "tracked").read_text() == "restart work\n"
    assert (path / "new").exists()


@pytest.mark.parametrize(
    "damage", ["task", "branch", "identity", "refs", "array", "state"]
)
def test_unrelated_or_corrupt_checkpoint_is_refused(repo, monkeypatch, damage):
    _fail(repo, monkeypatch)
    record = checkpoints.load("aabbccdd", "feature/resume")
    path = checkpoints._path("aabbccdd")
    if damage == "array":
        path.write_text("[]")
    else:
        record[damage] = {
            "task": "other",
            "branch": "feature/other",
            "identity": {},
            "refs": ["HEAD;rm"],
            "state": "unknown",
        }[damage]
        path.write_text(json.dumps(record))
    assert not checkpoints.status("aabbccdd", "feature/resume")["can_resume"]
    with pytest.raises((ValueError, RuntimeError)):
        checkpoints.restore("aabbccdd", "feature/resume")


def test_no_progress_refuses_resume(repo):
    _, git = repo
    git("checkout", "-b", "feature/resume")
    checkpoints.begin("aabbccdd", "feature/resume")
    assert not checkpoints.status("aabbccdd", "feature/resume")["can_resume"]


def test_live_provider_marker_prevents_recovery(repo):
    import os

    provider_process._marker().write_text(json.dumps({"group": os.getpgrp()}))
    with pytest.raises(RuntimeError, match="still be running"):
        provider_process.require_no_writer()


def test_branch_move_and_recovery_ref_replacement_refuse(repo, monkeypatch):
    _, git = repo
    _fail(repo, monkeypatch, committed=True)
    saved = checkpoints.load("aabbccdd", "feature/resume")
    stash_ref = next(ref for ref in saved["refs"] if ref.endswith("/stash"))
    git("update-ref", stash_ref, "main")
    assert not checkpoints.status("aabbccdd", "feature/resume")["can_resume"]


def test_publication_failure_retains_committed_progress(repo, monkeypatch):
    path, git = repo
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume")
        monkeypatch.setattr(
            workflow,
            "run_implementation_agent",
            lambda *_: (
                (path / "new").write_text("work") and CommandResult([], 0, "done")
            ),
        )
        workflow.implement("approved", "feature/resume")
        git("add", ".")
        git("commit", "-m", "implementation completed before PR failure")
        git_recovery.cleanup_recovery()
    git("checkout", "main")
    assert checkpoints.status("aabbccdd", "feature/resume")["can_resume"]


def test_interrupted_ci_preserves_checkpoint_but_refuses_duplicate_pr(
    repo, monkeypatch
):
    path, git = repo
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume")
        monkeypatch.setattr(
            workflow,
            "run_implementation_agent",
            lambda *_: (
                (path / "new").write_text("work") and CommandResult([], 0, "done")
            ),
        )
        workflow.implement("approved", "feature/resume")
        git("add", ".")
        git("commit", "-m", "published")
        git_recovery.mark_published("https://example.com/pr/1")
        (path / "new").write_text("partial CI repair")
        git_recovery.cleanup_recovery()
    saved = checkpoints.load("aabbccdd", "feature/resume")
    assert saved["state"] == "saved"
    stash = next(ref for ref in saved["refs"] if ref.endswith("/stash"))
    assert git("show", stash + ":new") == "partial CI repair"
    availability = checkpoints.status("aabbccdd", "feature/resume")
    assert not availability["can_resume"]
    assert "PR exists" in availability["resume_reason"]


def test_failure_before_branch_setup_does_not_block_repository(repo, monkeypatch):
    path, git = repo
    actual = workflow.run

    def fail_pull(args, *a, **kw):
        if args[:2] == ["git", "pull"]:
            raise RuntimeError("network unavailable")
        return actual(args, *a, **kw)

    monkeypatch.setattr(workflow, "run", fail_pull)
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume")
        with pytest.raises(RuntimeError, match="network unavailable"):
            workflow.implement("approved", "feature/resume")
        assert git_recovery.cleanup_recovery()
    assert not (path / ".git/ai-agent-recovery-blocked").exists()
    assert not checkpoints.status("aabbccdd", "feature/resume")["can_resume"]
    assert git("branch", "--show-current") == "main"


def test_double_cleanup_keeps_staged_untracked_checkpoint(repo, monkeypatch):
    _fail(repo, monkeypatch)
    record = checkpoints.load("aabbccdd", "feature/resume")
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume", True)
        monkeypatch.setattr(
            workflow,
            "run_implementation_agent",
            lambda *_: CommandResult([], 0, "done"),
        )
        workflow.resume_implementation("approved", "feature/resume")
        git_recovery.cleanup_recovery()
        first = checkpoints.load("aabbccdd", "feature/resume")
        git_recovery.cleanup_recovery()
        assert checkpoints.load("aabbccdd", "feature/resume") == first
        assert first["refs"] != record["refs"]
        assert any(ref.endswith("/stash") for ref in first["refs"])


def test_crash_then_other_task_preserves_original_ownership(repo, monkeypatch):
    path, git = repo
    git("checkout", "-b", "feature/resume")
    checkpoints.begin("aabbccdd", "feature/resume")
    (path / "tracked").write_text("crashed work\n")
    (path / "untracked").write_text("crashed new\n")

    def other_provider(*_):
        (path / "other").write_text("other task\n")
        return CommandResult([], 0, "done")

    monkeypatch.setattr(workflow, "run_implementation_agent", other_provider)
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("11223344", "feature/other")
        workflow.implement("other", "feature/other")
        git_recovery.cleanup_recovery()
    git("checkout", "main")
    assert checkpoints.status("aabbccdd", "feature/resume")["can_resume"]
    monkeypatch.setattr(
        workflow, "run_implementation_agent", lambda *_: CommandResult([], 0, "done")
    )
    with git_recovery.recovery_session():
        git_recovery.bind_checkpoint("aabbccdd", "feature/resume", True)
        workflow.resume_implementation("original", "feature/resume")
    assert (path / "tracked").read_text() == "crashed work\n"
    assert (path / "untracked").read_text() == "crashed new\n"
    assert not (path / "other").exists()


def test_task_binding_uses_repo_and_exact_plan(repo):
    from ai_agent.tasks import remember_run

    data = {
        "tasks": [
            {
                "id": "old",
                "repo": "other",
                "branch": "feature/x",
                "stage": "planned",
                "plan_id": "p1",
            },
            {
                "id": "past",
                "repo": "right",
                "branch": "feature/x",
                "stage": "planned",
                "plan_id": "p0",
            },
            {
                "id": "correct",
                "repo": "right",
                "branch": "feature/x",
                "stage": "planned",
                "plan_id": "p1",
            },
        ]
    }
    item = {
        "repository_identity": {"project_name": "right"},
        "branch_name": "feature/x",
        "plan_id": "p1",
        "codex_prompt": "approved",
    }
    remember_run(data, item)
    assert item["checkpoint_task"] == "correct"
    assert "run" not in data["tasks"][0]
    assert "run" not in data["tasks"][1]
