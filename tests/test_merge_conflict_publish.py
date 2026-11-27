"""Real-Git tests for publishing a validated merge resolution."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from ai_agent import merge_conflict_publish as mcp
from ai_agent.merge_conflicts import RESOLVED, UP_TO_DATE, MergeOutcome


def git(cwd, *args):
    """Run git and return stripped stdout; raises on a non-zero exit."""
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def _init_repo(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")


def _init_bare(path):
    subprocess.run(
        ["git", "init", "-q", "--bare", str(path)],
        check=True,
        capture_output=True,
    )


def _remote_head(source, remote, ref):
    output = git(source, "ls-remote", str(remote), f"refs/heads/{ref}")
    if not output:
        return None
    return output.split()[0]


def _resolved_tree(source, head):
    """Build a distinct resolved tree without touching the real index."""
    index_file = source / ".git" / "publish-test-index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    try:
        subprocess.run(
            ["git", "-C", str(source), "read-tree", head],
            check=True,
            capture_output=True,
            env=env,
        )
        blob = (
            subprocess.run(
                ["git", "-C", str(source), "hash-object", "-w", "--stdin"],
                input=b"resolved\n",
                check=True,
                capture_output=True,
                env=env,
            )
            .stdout.decode()
            .strip()
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},shared.txt",
            ],
            check=True,
            capture_output=True,
            env=env,
        )
        return subprocess.run(
            ["git", "-C", str(source), "write-tree"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()
    finally:
        index_file.unlink(missing_ok=True)


def _make_unmerged(source, path):
    """Stage three index stages for a path to simulate an unresolved merge."""
    blob = (
        subprocess.run(
            ["git", "-C", str(source), "hash-object", "-w", "--stdin"],
            input=b"unresolved\n",
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    info = (
        f"100644 {blob} 1\t{path}\n100644 {blob} 2\t{path}\n100644 {blob} 3\t{path}\n"
    )
    subprocess.run(
        ["git", "-C", str(source), "update-index", "--index-info"],
        input=info.encode(),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A source repo plus a disposable bare remote with head and base refs."""
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    _init_repo(source)
    # Only tests use a local file remote; production pushes to a real remote.
    git(source, "config", "protocol.file.allow", "always")
    _init_bare(remote)

    (source / "shared.txt").write_text("base\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "ancestor")

    git(source, "checkout", "-qb", "feature")
    (source / "shared.txt").write_text("head\n")
    git(source, "commit", "-qam", "head")
    head = git(source, "rev-parse", "HEAD")

    git(source, "checkout", "-q", "main")
    (source / "shared.txt").write_text("base2\n")
    git(source, "commit", "-qam", "base")
    base = git(source, "rev-parse", "HEAD")

    git(source, "push", "-q", str(remote), f"{head}:refs/heads/feature")
    git(source, "push", "-q", str(remote), f"{base}:refs/heads/main")

    return SimpleNamespace(
        source=source,
        remote=str(remote),
        head_ref="feature",
        base_ref="main",
        head=head,
        base=base,
        tree=_resolved_tree(source, head),
    )


def _outcome(repo, state=RESOLVED):
    return MergeOutcome(state=state, tree=repo.tree, parents=(repo.head, repo.base))


def test_publish_lands_one_merge_commit_on_head(repo):
    outcome = _outcome(repo)

    sha = mcp.publish_merge_commit(
        repo.source, repo.remote, repo.head_ref, repo.head, repo.base, outcome
    )

    assert sha is not None
    assert sha == _remote_head(repo.source, repo.remote, repo.head_ref)
    assert git(repo.source, "rev-parse", f"{sha}^1") == repo.head
    assert git(repo.source, "rev-parse", f"{sha}^2") == repo.base
    assert git(repo.source, "rev-parse", f"{sha}^{{tree}}") == repo.tree
    assert git(repo.source, "log", "-1", "--format=%s", sha) == "Fix merge conflicts"
    # The base branch is never updated.
    assert _remote_head(repo.source, repo.remote, repo.base_ref) == repo.base


def test_up_to_date_outcome_returns_noop(repo):
    outcome = MergeOutcome(state=UP_TO_DATE, tree=None, parents=(repo.head,))

    result = mcp.publish_merge_commit(
        repo.source, repo.remote, repo.head_ref, repo.head, repo.base, outcome
    )

    assert result is None
    assert _remote_head(repo.source, repo.remote, repo.head_ref) == repo.head


def test_moved_head_ref_is_rejected(repo):
    outcome = _outcome(repo)
    git(
        repo.source,
        "push",
        "-q",
        "--force",
        repo.remote,
        f"{repo.base}:refs/heads/{repo.head_ref}",
    )

    with pytest.raises(mcp.MergeConflictPublishError, match="moved"):
        mcp.publish_merge_commit(
            repo.source, repo.remote, repo.head_ref, repo.head, repo.base, outcome
        )

    assert _remote_head(repo.source, repo.remote, repo.head_ref) == repo.base


def test_unresolved_index_is_rejected(repo):
    outcome = _outcome(repo)
    _make_unmerged(repo.source, "shared.txt")

    with pytest.raises(mcp.MergeConflictPublishError, match="Unresolved index"):
        mcp.publish_merge_commit(
            repo.source, repo.remote, repo.head_ref, repo.head, repo.base, outcome
        )

    assert _remote_head(repo.source, repo.remote, repo.head_ref) == repo.head


def test_failed_check_leaves_remote_head_unchanged(repo):
    outcome = _outcome(repo)

    def failing_verify(source, sha, tree, parents):
        raise mcp.MergeConflictPublishError("verification failed")

    with pytest.raises(mcp.MergeConflictPublishError, match="verification failed"):
        mcp.publish_merge_commit(
            repo.source,
            repo.remote,
            repo.head_ref,
            repo.head,
            repo.base,
            outcome,
            verify=failing_verify,
        )

    assert _remote_head(repo.source, repo.remote, repo.head_ref) == repo.head


def test_outcome_parents_mismatch_is_rejected(repo):
    outcome = MergeOutcome(state=RESOLVED, tree=repo.tree, parents=(repo.head,))

    with pytest.raises(mcp.MergeConflictPublishError, match="parents"):
        mcp.publish_merge_commit(
            repo.source, repo.remote, repo.head_ref, repo.head, repo.base, outcome
        )

    assert _remote_head(repo.source, repo.remote, repo.head_ref) == repo.head


def test_head_move_between_check_and_push_is_rejected(repo, monkeypatch):
    tree = _resolved_tree(repo.source, repo.head)
    outcome = MergeOutcome(RESOLVED, tree, (repo.head, repo.base))
    original = mcp._ls_remote_head

    def race(source, remote, head_ref):
        observed = original(source, remote, head_ref)
        git(source, "push", "--force", remote, f"{repo.base}:refs/heads/{head_ref}")
        return observed

    monkeypatch.setattr(mcp, "_ls_remote_head", race)
    with pytest.raises(mcp.MergeConflictPublishError, match="Push failed"):
        mcp.publish_merge_commit(
            repo.source, repo.remote, "feature", repo.head, repo.base, outcome
        )
    assert _remote_head(repo.source, repo.remote, "feature") == repo.base
