"""Exercise the complete merge flow against disposable Git repositories."""

from dataclasses import replace

import pytest

from ai_agent import git_recovery, isolation, shell, workflow
from ai_agent import merge_conflict_flow as flow
from ai_agent.merge_conflict import MergeConflictTarget
from ai_agent.projects import Project
from tests.test_merge_conflict_publish import git
from tests.test_merge_conflict_publish import repo as git_repo

repo = git_repo


@pytest.fixture
def wired(repo, monkeypatch):
    project = Project("fixture", repo.source, "owner/repo", "main", "fixture")
    target = MergeConflictTarget(
        1,
        "https://github.com/owner/repo/pull/1",
        "Conflicted PR",
        "main",
        repo.base,
        "feature",
        repo.head,
        "",
    )
    monkeypatch.setattr(git_recovery, "active_project", lambda: project)
    monkeypatch.setattr(shell, "active_project", lambda: project)
    monkeypatch.setattr(flow, "clone_url", lambda _: repo.remote)
    monkeypatch.setattr(flow, "validate_merge_conflict_pr", lambda _: target)
    monkeypatch.setattr(isolation, "_prepare_ownership", lambda _: None)
    checks = []
    monkeypatch.setattr(
        flow, "check_merge", lambda checkout, _: checks.append(checkout)
    )

    def provider(prompt, agent):
        checkout = isolation.current_sandbox().path
        assert checkout != project.repo_path
        assert "shared.txt" in prompt
        assert git(checkout, "rev-parse", "HEAD") == repo.head
        (checkout / "shared.txt").write_text("combined intent\n")
        git(checkout, "add", "shared.txt")

    monkeypatch.setattr(workflow, "run_implementation_agent", provider)
    return project, target, checks


def test_complete_flow_updates_only_pr_head_and_preserves_dirty_checkout(repo, wired):
    project, target, checks = wired
    (repo.source / "local.txt").write_text("owner work\n")
    before = git(repo.source, "status", "--porcelain")
    result = flow.fix_pinned_conflicts(project, target, "codex")
    assert "Updated PR #1" in result
    head = git(repo.source, "ls-remote", repo.remote, "refs/heads/feature").split()[0]
    assert git(repo.source, "rev-parse", f"{head}^1") == repo.head
    assert git(repo.source, "rev-parse", f"{head}^2") == repo.base
    assert git(repo.source, "show", f"{head}:shared.txt") == "combined intent"
    assert (
        git(repo.source, "ls-remote", repo.remote, "refs/heads/main").split()[0]
        == repo.base
    )
    assert git(repo.source, "status", "--porcelain") == before
    assert len(checks) == 1 and not checks[0].exists()


def test_failed_checks_never_publish(repo, wired, monkeypatch):
    project, target, _ = wired

    def fail(*_):
        raise RuntimeError("tests failed")

    monkeypatch.setattr(flow, "check_merge", fail)
    with pytest.raises(RuntimeError, match="tests failed"):
        flow.fix_pinned_conflicts(project, target)
    assert (
        git(repo.source, "ls-remote", repo.remote, "refs/heads/feature").split()[0]
        == repo.head
    )


def test_moved_base_never_publishes(repo, wired, monkeypatch):
    project, target, _ = wired
    monkeypatch.setattr(
        flow, "validate_merge_conflict_pr", lambda _: replace(target, base_sha="a" * 40)
    )
    with pytest.raises(RuntimeError, match="branches moved"):
        flow.fix_pinned_conflicts(project, target)
    assert (
        git(repo.source, "ls-remote", repo.remote, "refs/heads/feature").split()[0]
        == repo.head
    )


def test_repository_lease_rejects_another_run(repo, wired):
    project, target, _ = wired
    with git_recovery.repository_lease():
        with pytest.raises(RuntimeError, match="active run"):
            flow.fix_pinned_conflicts(project, target)


def test_provider_checkout_supports_a_clone_git_directory(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    home = tmp_path / "worker-home"
    monkeypatch.setattr(isolation, "WORKER_ISOLATION_ENABLED", True)
    monkeypatch.setattr(isolation, "WORKER_HOME", home)
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(isolation, "_uid", lambda _: 1000)
    monkeypatch.setattr(isolation, "_gid", lambda _: 1000)
    owned = []
    monkeypatch.setattr(isolation, "_chown_tree", lambda path, *_: owned.append(path))
    with isolation.provider_checkout(tmp_path, "a" * 40):
        assert isolation.current_sandbox().path == tmp_path
    assert tmp_path / ".git" in owned
    assert isolation.current_sandbox() is None
