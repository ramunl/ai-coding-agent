"""Recovery integration tests using local Git repositories only."""

import subprocess
from types import SimpleNamespace

import pytest

from ai_agent import git_recovery as recovery
from ai_agent import workflow
from ai_agent.shell import CommandResult


@pytest.fixture
def repository(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "tracked").write_text("original\n")
    git("add", ".")
    git("commit", "-m", "initial")
    git("remote", "add", "origin", str(tmp_path))
    project = SimpleNamespace(repo_path=tmp_path, base_branch="main")
    monkeypatch.setattr(recovery, "active_project", lambda: project)
    monkeypatch.setattr(workflow, "active_project", lambda: project)
    from ai_agent import shell

    monkeypatch.setattr(shell, "active_project", lambda: project)
    return tmp_path, git


@pytest.mark.parametrize(
    "entry", ["implement", "repair_implementation", "repair_pull_request_branch"]
)
@pytest.mark.parametrize("kind", ["unstaged", "staged", "untracked", "intent"])
def test_dirty_preflight(repository, monkeypatch, entry, kind):
    path, git = repository
    target = "new" if kind in ("untracked", "intent") else "tracked"
    (path / target).write_text("owner content")
    if kind in ("staged", "intent"):
        git("add", *(["-N"] if kind == "intent" else []), target)
    before = git("status", "--porcelain")
    monkeypatch.setattr(
        workflow, "run_implementation_agent", lambda *a: pytest.fail("provider called")
    )
    with pytest.raises(RuntimeError, match="dirty"):
        getattr(workflow, entry)("task", "feature/test")
    assert git("status", "--porcelain") == before
    assert git("branch", "--show-current") == "main"
    assert not git("stash", "list")


@pytest.mark.parametrize("intent", [False, True])
def test_failed_task_is_excluded_from_next_commit(repository, monkeypatch, intent):
    path, git = repository

    def fail(*args):
        (path / "tracked").write_text("failed staged\n")
        git("add", "tracked")
        (path / "tracked").write_text("failed unstaged\n")
        (path / "failed-new").write_text("failed new\n")
        if intent:
            git("add", "-N", "failed-new")
        raise RuntimeError("provider failed")

    monkeypatch.setattr(workflow, "run_implementation_agent", fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        workflow.implement("first", "feature/test")
    stash = git("rev-parse", "refs/stash")
    assert git("show", f"{stash}:tracked") == "failed unstaged"
    assert git("show", f"{stash}^2:tracked") == "failed staged"
    assert git("show", f"{stash}^3:failed-new") == "failed new"
    assert not git("status", "--porcelain")

    def succeed(*args):
        (path / "success").write_text("second task\n")
        return CommandResult([], 0, "done")

    monkeypatch.setattr(workflow, "run_implementation_agent", succeed)
    workflow.implement("second", "feature/test")
    workflow.push("feature/test", "second")
    assert git("diff", "--name-only", "main", "HEAD") == "success"
    assert git("rev-parse", "refs/stash") == stash


def test_preservation_failure_blocks_retry(repository, monkeypatch):
    path, git = repository
    original = recovery.run

    def broken(args, **kwargs):
        if args[:3] == ["git", "stash", "push"]:
            raise RuntimeError("secret command output")
        return original(args, **kwargs)

    monkeypatch.setattr(recovery, "run", broken)

    def fail(*args):
        (path / "tracked").write_text("failed")
        raise RuntimeError("provider failed")

    monkeypatch.setattr(workflow, "run_implementation_agent", fail)
    with pytest.raises(RuntimeError, match="provider failed") as error:
        workflow.implement("task", "feature/test")
    assert "Cleanup failed" in str(error.value.__notes__)
    assert "secret" not in str(error.value.__notes__)
    assert git("branch", "--show-current") == "feature/test"
    assert (path / "tracked").read_text() == "failed"
    with pytest.raises(RuntimeError, match="recovery is incomplete"):
        workflow.implement("task", "feature/test")


def test_submodule_blocks_preflight_and_cleanup(repository, tmp_path):
    path, git = repository
    child = tmp_path / "child-source"
    subprocess.run(
        ["git", "clone", str(path), str(child)], check=True, capture_output=True
    )
    git("-c", "protocol.file.allow=always", "submodule", "add", str(child), "child")
    # Source is deliberately ignored, like local build data.
    (path / ".gitignore").write_text("child-source/\n")
    git("add", ".")
    git("commit", "-m", "submodule")
    (path / "child" / "untracked").write_text("owner")
    git("config", "submodule.child.ignore", "all")
    with pytest.raises(RuntimeError, match="dirty"):
        recovery.require_clean_checkout()
    with pytest.raises(RuntimeError, match="Dirty submodule"):
        recovery.preserve_changes()
    assert (path / "child" / "untracked").read_text() == "owner"


def test_concurrent_lease_is_rejected(repository):
    with recovery.recovery_session() as first:
        recovery._lease(first)
        with recovery.recovery_session() as second:
            with pytest.raises(RuntimeError, match="active run"):
                recovery._lease(second)


@pytest.mark.parametrize(
    "entry", ["repair_implementation", "repair_pull_request_branch"]
)
def test_repair_failure_preserves_edits(repository, monkeypatch, entry):
    path, git = repository
    git("branch", "feature/repair")
    git("fetch", "origin", "feature/repair")

    def fail(*args):
        (path / "tracked").write_text("repair edits")
        (path / "repair-new").write_text("new repair edits")
        raise RuntimeError("repair provider failed")

    monkeypatch.setattr(workflow, "run_implementation_agent", fail)
    with pytest.raises(RuntimeError, match="repair provider failed"):
        getattr(workflow, entry)("repair", "feature/repair")
    assert not git("status", "--porcelain")
    assert git("show", "stash:tracked") == "repair edits"
    assert git("show", "stash^3:repair-new") == "new repair edits"


def test_retry_keeps_old_commits_stashes_and_ignored_files(repository, monkeypatch):
    path, git = repository
    (path / ".gitignore").write_text("ignored\n")
    git("add", ".gitignore")
    git("commit", "-m", "ignore")
    (path / "ignored").write_text("local secret")
    (path / "tracked").write_text("owner stash")
    git("stash", "push", "-m", "owner")
    owner_stash = git("rev-parse", "stash")
    git("checkout", "-b", "feature/retry")
    (path / "tracked").write_text("old commit")
    git("add", ".")
    git("commit", "-m", "failed run commit")
    old_commit = git("rev-parse", "HEAD")

    def fail(*args):
        (path / "tracked").write_text("failed retry")
        raise RuntimeError("provider failed")

    monkeypatch.setattr(workflow, "run_implementation_agent", fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        workflow.implement("retry", "feature/retry")
    refs = git("for-each-ref", "--format=%(objectname)", "refs/ai-agent-recovery")
    assert old_commit in refs.splitlines()
    assert git("rev-parse", "stash@{1}") == owner_stash
    assert (path / "ignored").read_text() == "local secret"


def test_cleanup_verification_failure_blocks_even_clean_checkout(
    repository, monkeypatch
):
    path, git = repository
    original = recovery.run

    def bad_verification(args, **kwargs):
        if args[:3] == ["git", "log", "-1"]:
            return CommandResult(args, 0, "old stash")
        return original(args, **kwargs)

    monkeypatch.setattr(recovery, "run", bad_verification)

    def fail(*args):
        (path / "tracked").write_text("failed edits")
        raise RuntimeError("provider failed")

    monkeypatch.setattr(workflow, "run_implementation_agent", fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        workflow.implement("task", "feature/test")
    assert not git("status", "--porcelain")
    assert git("show", "stash:tracked") == "failed edits"
    with pytest.raises(RuntimeError, match="recovery is incomplete"):
        workflow.implement("next", "feature/test")
    assert git("branch", "--show-current") == "feature/test"


def test_checkout_does_not_overwrite_ignored_file(repository):
    path, git = repository
    (path / "collision").write_text("base file")
    git("add", ".")
    git("commit", "-m", "base file")
    git("checkout", "-b", "feature/test")
    git("rm", "collision")
    (path / ".gitignore").write_text("collision\n")
    git("add", ".")
    git("commit", "-m", "ignore file")
    (path / "collision").write_text("private ignored content")
    with pytest.raises(RuntimeError):
        workflow.return_to_base_branch()
    assert (path / "collision").read_text() == "private ignored content"
    assert git("branch", "--show-current") == "feature/test"


def test_execution_lease_covers_publication_and_cancellation(repository):
    import asyncio

    async def scenario():
        ready = asyncio.Event()
        release = asyncio.Event()

        @recovery.guarded_run
        def implementation():
            pass

        @recovery.serialized_execution
        async def execution():
            implementation()
            ready.set()
            # Represents the interval after the provider and before publication.
            await release.wait()
            assert recovery.cleanup_recovery()

        running = asyncio.create_task(execution())
        await ready.wait()
        with pytest.raises(RuntimeError, match="active run"):
            implementation()
        running.cancel()
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="active run"):
            implementation()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        implementation()

    asyncio.run(scenario())
