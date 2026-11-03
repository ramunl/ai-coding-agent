"""Real Git regressions for safe prerequisite synchronization."""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent import dependency_checkout as sync
from ai_agent.context_models import RepositoryIdentity


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-qb", "main")
    git(remote, "config", "user.email", "test@example.org")
    git(remote, "config", "user.name", "Test")
    (remote / "file").write_text("base")
    git(remote, "add", "file")
    git(remote, "commit", "-qm", "base")
    repo = tmp_path / "checkout"
    subprocess.check_call(["git", "clone", "-q", str(remote), str(repo)])
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    (remote / "file").write_text("merged prerequisite")
    git(remote, "commit", "-qam", "merge prerequisite")
    merged = git(remote, "rev-parse", "HEAD")
    identity = RepositoryIdentity("project", str(repo), "owner/project")
    project = SimpleNamespace(
        name="project",
        repo_path=repo,
        github_repository="owner/project",
        base_branch="main",
    )
    monkeypatch.setattr(sync, "get_project", lambda name: project)
    original = sync._git

    def local_git(path, *args, **kwargs):
        # Keep fetch local while exercising the production identity checks.
        if args == ("remote", "get-url", "origin"):
            return "git@github.com:owner/project.git"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sync, "_git", local_git)
    return repo, remote, sync.StaleCheckoutError(identity, merged), local_git


def test_stale_clean_base_fast_forwards_to_merged_prerequisite(checkout):
    repo, _, stale, _ = checkout
    asyncio.run(sync.synchronize(stale, {}))
    assert git(repo, "rev-parse", "HEAD") == stale.commit
    assert (repo / "file").read_text() == "merged prerequisite"


@pytest.mark.parametrize(
    "work", ["untracked", "unstaged", "staged", "branch", "detached", "diverged"]
)
def test_unfinished_work_and_non_base_checkouts_are_preserved(checkout, work):
    repo, _, stale, _ = checkout
    if work == "untracked":
        (repo / "private").write_text("keep me")
    elif work in {"unstaged", "staged"}:
        (repo / "file").write_text("keep my edits")
        if work == "staged":
            git(repo, "add", "file")
    elif work == "branch":
        git(repo, "checkout", "-qb", "feature/work")
    elif work == "detached":
        git(repo, "checkout", "--detach", "-q")
    else:
        (repo / "local").write_text("local commit")
        git(repo, "add", "local")
        git(repo, "commit", "-qm", "local")
    before = git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain")
    with pytest.raises(ValueError):
        asyncio.run(sync.synchronize(stale, {}))
    assert before == (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "status", "--porcelain"),
    )
    if work in {"unstaged", "staged"}:
        assert (repo / "file").read_text() == "keep my edits"


@pytest.mark.parametrize(
    "flag",
    [
        "active_execution",
        "planning_active",
        "planning_task",
        "task_queue",
        "queue_runner_active",
        "thinking",
        "pending_plan",
        "pending_implementation",
    ],
)
def test_busy_agent_never_fetches_or_changes_checkout(checkout, monkeypatch, flag):
    repo, _, stale, _ = checkout
    before = git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(
        sync, "_git", lambda *a, **kw: pytest.fail("Git ran while busy")
    )
    with pytest.raises(ValueError, match="finish active or pending work"):
        asyncio.run(sync.synchronize(stale, {flag: True}))
    assert git(repo, "rev-parse", "HEAD") == before


@pytest.mark.parametrize(
    "change",
    [
        "busy",
        "head",
        "dirty",
        "registration",
        "remote",
        "fetch_failure",
        "missing_merge",
        "registration_after_fetch",
    ],
)
def test_changes_during_fetch_and_invalid_identity_prevent_merge(
    checkout, monkeypatch, change
):
    repo, _, stale, original = checkout
    data = {}
    before = git(repo, "rev-parse", "HEAD")
    if change == "registration":
        monkeypatch.setattr(
            sync,
            "get_project",
            lambda n: SimpleNamespace(
                name="project",
                repo_path=Path("/other"),
                github_repository="owner/project",
                base_branch="main",
            ),
        )
    if change == "missing_merge":
        stale.commit = "a" * 40
    calls = []

    def concurrent(path, *args, **kwargs):
        calls.append(args)
        if change == "remote" and args == ("remote", "get-url", "origin"):
            return "git@github.com:other/project.git"
        if args[0] == "fetch":
            if change == "fetch_failure":
                raise ValueError("Git unavailable")
            result = original(path, *args, **kwargs)
            if change == "busy":
                data["active_execution"] = True
            elif change == "head":
                git(repo, "commit", "--allow-empty", "-qm", "concurrent work")
            elif change == "dirty":
                (repo / "unfinished").write_text("do not delete")
            elif change == "registration_after_fetch":
                monkeypatch.setattr(
                    sync,
                    "get_project",
                    lambda n: SimpleNamespace(
                        name="project",
                        repo_path=Path("/other"),
                        github_repository="owner/project",
                        base_branch="main",
                    ),
                )
            return result
        return original(path, *args, **kwargs)

    monkeypatch.setattr(sync, "_git", concurrent)
    with pytest.raises(ValueError):
        asyncio.run(sync.synchronize(stale, data))
    assert not any(args[0] == "merge" for args in calls)
    if change != "head":
        assert git(repo, "rev-parse", "HEAD") == before
