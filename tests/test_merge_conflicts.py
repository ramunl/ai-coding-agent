"""Real-Git tests for disposable merge-conflict investigation and resolution."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent import merge_conflicts as mc
from ai_agent.merge_conflict_stages import (
    Conflict,
    IndexEntry,
    IndexStage,
    parse_index,
    parse_unmerged,
)


def git(cwd, *args):
    """Run git and return stripped stdout; raises on a non-zero exit."""
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


def _init_repo(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")


def _snapshot(path):
    """The source state a merge-conflict fix must leave untouched."""
    return (
        git(path, "symbolic-ref", "--short", "HEAD"),
        git(path, "rev-parse", "HEAD"),
        subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
    )


def _dirty(path):
    """Add staged, unstaged, and untracked edits to a source checkout."""
    (path / "clean.txt").write_text("dirty edit\n")
    (path / "staged.txt").write_text("staged\n")
    git(path, "add", "staged.txt")
    (path / "untracked.txt").write_text("untracked\n")


@pytest.fixture
def conflicted(tmp_path):
    """A source repo whose head and base both edit ``shared.txt``."""
    source = tmp_path / "source"
    _init_repo(source)
    (source / "shared.txt").write_text("base\n")
    (source / "clean.txt").write_text("clean base\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "ancestor")
    ancestor = git(source, "rev-parse", "HEAD")
    base_blob = git(source, "rev-parse", "HEAD:shared.txt")

    git(source, "checkout", "-qb", "feature")
    (source / "shared.txt").write_text("head\n")
    (source / "clean.txt").write_text("clean head\n")
    git(source, "commit", "-qam", "head")
    head = git(source, "rev-parse", "HEAD")
    head_blob = git(source, "rev-parse", "HEAD:shared.txt")

    git(source, "checkout", "-q", "main")
    (source / "shared.txt").write_text("base2\n")
    git(source, "commit", "-qam", "base2")
    base = git(source, "rev-parse", "HEAD")
    base2_blob = git(source, "rev-parse", "HEAD:shared.txt")

    return SimpleNamespace(
        source=source,
        head=head,
        base=base,
        shared="shared.txt",
        clean="clean.txt",
        ancestor=ancestor,
        base_blob=base_blob,
        head_blob=head_blob,
        base2_blob=base2_blob,
    )


@pytest.fixture
def add_add(tmp_path):
    """A source repo whose head and base both add ``new.txt``."""
    source = tmp_path / "source"
    _init_repo(source)
    (source / "keep.txt").write_text("keep\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "ancestor")

    git(source, "checkout", "-qb", "feature")
    (source / "new.txt").write_text("head version\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "head adds")
    head = git(source, "rev-parse", "HEAD")

    git(source, "checkout", "-q", "main")
    (source / "new.txt").write_text("base version\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "base adds")
    base = git(source, "rev-parse", "HEAD")

    return SimpleNamespace(source=source, head=head, base=base, new="new.txt")


@pytest.fixture
def clean_repo(tmp_path):
    """A source repo whose head and base edit disjoint files."""
    source = tmp_path / "source"
    _init_repo(source)
    (source / "head.txt").write_text("head base\n")
    (source / "base.txt").write_text("base base\n")
    (source / "clean.txt").write_text("clean base\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "ancestor")

    git(source, "checkout", "-qb", "feature")
    (source / "head.txt").write_text("head change\n")
    git(source, "commit", "-qam", "head")
    head = git(source, "rev-parse", "HEAD")

    git(source, "checkout", "-q", "main")
    (source / "base.txt").write_text("base change\n")
    git(source, "commit", "-qam", "base")
    base = git(source, "rev-parse", "HEAD")

    return SimpleNamespace(source=source, head=head, base=base)


@pytest.fixture
def up_to_date(tmp_path):
    """A source repo whose head already contains the base commit."""
    source = tmp_path / "source"
    _init_repo(source)
    (source / "a.txt").write_text("base\n")
    (source / "clean.txt").write_text("clean base\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "base")
    base = git(source, "rev-parse", "HEAD")

    git(source, "checkout", "-qb", "feature")
    (source / "a.txt").write_text("head change\n")
    git(source, "commit", "-qam", "head")
    head = git(source, "rev-parse", "HEAD")

    return SimpleNamespace(source=source, head=head, base=base)


@pytest.fixture
def tracked_checkouts(tmp_path, monkeypatch):
    """Direct disposable clones under tmp_path and record their creation."""
    created = []
    real_mkdtemp = mc.tempfile.mkdtemp

    def fake_mkdtemp(prefix="ai-agent-merge-"):
        path = real_mkdtemp(prefix=prefix, dir=str(tmp_path))
        created.append(path)
        return path

    monkeypatch.setattr(mc.tempfile, "mkdtemp", fake_mkdtemp)
    return created


def _resolve(record):
    """A stub resolver that records conflicts and stages a simple resolution."""

    def resolver(checkout, conflicts):
        record.extend(conflicts)
        for conflict in conflicts:
            (checkout / conflict.path).write_text(f"resolved:{conflict.path}\n")
        git(checkout, "add", "-A")

    return resolver


def _never():
    """A resolver that fails the test if it is invoked."""

    def resolver(checkout, conflicts):
        pytest.fail("resolver should not have been invoked")

    return resolver


def test_parse_index_preserves_modes_and_blobs():
    records = (
        "100644 aaaa 0\tplain.txt\0"
        "100755 bbbb 2\tscript.sh\0"
        "120000 cccc 3\tlink\0"
        "160000 dddd 1\tsub\0"
    )
    entries = parse_index(records)
    assert entries == [
        IndexEntry(path="plain.txt", stage=0, mode="100644", blob="aaaa"),
        IndexEntry(path="script.sh", stage=2, mode="100755", blob="bbbb"),
        IndexEntry(path="link", stage=3, mode="120000", blob="cccc"),
        IndexEntry(path="sub", stage=1, mode="160000", blob="dddd"),
    ]


def test_parse_unmerged_groups_stages_and_omits_absent_ones():
    records = (
        "100644 aaaa 1\tf.txt\0"
        "100755 bbbb 2\tf.txt\0"
        "100644 cccc 3\tf.txt\0"
        "160000 dddd 2\tsub\0"
        "120000 eeee 3\tsub\0"
    )
    conflicts = parse_unmerged(records)
    assert conflicts == [
        Conflict(
            path="f.txt",
            stage1=IndexStage("100644", "aaaa"),
            stage2=IndexStage("100755", "bbbb"),
            stage3=IndexStage("100644", "cccc"),
        ),
        Conflict(
            path="sub",
            stage1=None,
            stage2=IndexStage("160000", "dddd"),
            stage3=IndexStage("120000", "eeee"),
        ),
    ]


def test_resolver_receives_stage_orientation(conflicted):
    record = []
    outcome = mc.resolve_merge_conflicts(
        conflicted.source, conflicted.head, conflicted.base, _resolve(record)
    )

    assert outcome.state == mc.RESOLVED
    assert outcome.parents == (conflicted.head, conflicted.base)
    assert len(record) == 1
    conflict = record[0]
    assert conflict.path == conflicted.shared
    assert conflict.stage1 == IndexStage("100644", conflicted.base_blob)
    assert conflict.stage2 == IndexStage("100644", conflicted.head_blob)
    assert conflict.stage3 == IndexStage("100644", conflicted.base2_blob)


def test_add_add_conflict_omits_stage1(add_add):
    record = []
    outcome = mc.resolve_merge_conflicts(
        add_add.source, add_add.head, add_add.base, _resolve(record)
    )

    assert outcome.state == mc.RESOLVED
    assert [conflict.path for conflict in record] == [add_add.new]
    conflict = record[0]
    assert conflict.stage1 is None
    assert conflict.stage2 is not None
    assert conflict.stage3 is not None


def test_resolution_returns_tree_available_in_source(conflicted):
    outcome = mc.resolve_merge_conflicts(
        conflicted.source, conflicted.head, conflicted.base, _resolve([])
    )

    assert outcome.tree is not None
    assert git(conflicted.source, "cat-file", "-t", outcome.tree) == "tree"
    blob = git(conflicted.source, "rev-parse", f"{outcome.tree}:{conflicted.shared}")
    assert git(conflicted.source, "cat-file", "-t", blob) == "blob"


def test_clean_merge_skips_resolver(clean_repo):
    outcome = mc.resolve_merge_conflicts(
        clean_repo.source, clean_repo.head, clean_repo.base, _never()
    )

    assert outcome.state == mc.CLEAN
    assert outcome.parents == (clean_repo.head, clean_repo.base)
    assert git(clean_repo.source, "cat-file", "-t", outcome.tree) == "tree"


def test_up_to_date_merge_returns_head_parent(up_to_date):
    outcome = mc.resolve_merge_conflicts(
        up_to_date.source, up_to_date.head, up_to_date.base, _never()
    )

    assert outcome.state == mc.UP_TO_DATE
    assert outcome.tree is None
    assert outcome.parents == (up_to_date.head,)


def test_invalid_commit_id_is_rejected(conflicted):
    with pytest.raises(mc.MergeConflictResolutionError):
        mc.resolve_merge_conflicts(
            conflicted.source, "z" * 40, conflicted.base, _never()
        )


def test_unavailable_commit_is_rejected(conflicted):
    with pytest.raises(mc.MergeConflictResolutionError):
        mc.resolve_merge_conflicts(
            conflicted.source, "0" * 40, conflicted.base, _never()
        )


def test_failed_merge_raises(conflicted, monkeypatch):
    failed = subprocess.CompletedProcess(
        ["git", "merge"], 128, stdout="", stderr="broken merge"
    )
    monkeypatch.setattr(mc, "_merge", lambda checkout: failed)

    with pytest.raises(mc.MergeConflictResolutionError, match="Merge failed"):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, _never()
        )


def test_resolution_with_unmerged_leftovers_raises(conflicted):
    def noop(checkout, conflicts):
        return None

    with pytest.raises(mc.MergeConflictResolutionError, match="Unmerged index"):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, noop
        )


def test_resolution_with_conflict_markers_raises(conflicted):
    def markers(checkout, conflicts):
        for conflict in conflicts:
            (checkout / conflict.path).write_text(
                "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> base\n"
            )
        git(checkout, "add", "-A")

    with pytest.raises(mc.MergeConflictResolutionError, match="conflict markers"):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, markers
        )


def test_resolution_with_untracked_file_raises(conflicted):
    def stray(checkout, conflicts):
        for conflict in conflicts:
            (checkout / conflict.path).write_text(f"resolved:{conflict.path}\n")
        (checkout / "stray.txt").write_text("left behind\n")
        git(checkout, "add", "--", *(conflict.path for conflict in conflicts))

    with pytest.raises(mc.MergeConflictResolutionError, match="untracked"):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, stray
        )


def test_resolution_touching_unrelated_path_raises(conflicted):
    def touch(checkout, conflicts):
        for conflict in conflicts:
            (checkout / conflict.path).write_text(f"resolved:{conflict.path}\n")
        (checkout / conflicted.clean).write_text("changed outside conflict set\n")
        git(checkout, "add", "-A")

    with pytest.raises(
        mc.MergeConflictResolutionError, match="outside the conflict set"
    ):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, touch
        )


def test_resolution_changing_head_raises(conflicted):
    def reset_head(checkout, conflicts):
        git(checkout, "reset", "--hard", conflicted.base)

    with pytest.raises(mc.MergeConflictResolutionError, match="changed HEAD"):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, reset_head
        )


def test_resolution_clearing_merge_head_raises(conflicted):
    def abort(checkout, conflicts):
        git(checkout, "merge", "--abort")

    with pytest.raises(mc.MergeConflictResolutionError, match="MERGE_HEAD"):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, abort
        )


def test_source_state_survives_resolved(conflicted):
    _dirty(conflicted.source)
    before = _snapshot(conflicted.source)

    mc.resolve_merge_conflicts(
        conflicted.source, conflicted.head, conflicted.base, _resolve([])
    )

    assert _snapshot(conflicted.source) == before


def test_source_state_survives_clean(clean_repo):
    _dirty(clean_repo.source)
    before = _snapshot(clean_repo.source)

    mc.resolve_merge_conflicts(
        clean_repo.source, clean_repo.head, clean_repo.base, _never()
    )

    assert _snapshot(clean_repo.source) == before


def test_source_state_survives_up_to_date(up_to_date):
    _dirty(up_to_date.source)
    before = _snapshot(up_to_date.source)

    mc.resolve_merge_conflicts(
        up_to_date.source, up_to_date.head, up_to_date.base, _never()
    )

    assert _snapshot(up_to_date.source) == before


def test_source_state_survives_failure(conflicted, monkeypatch):
    _dirty(conflicted.source)
    before = _snapshot(conflicted.source)
    failed = subprocess.CompletedProcess(
        ["git", "merge"], 128, stdout="", stderr="broken merge"
    )
    monkeypatch.setattr(mc, "_merge", lambda checkout: failed)

    with pytest.raises(mc.MergeConflictResolutionError):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, _never()
        )

    assert _snapshot(conflicted.source) == before


def test_cancellation_preserves_source_and_removes_checkout(
    conflicted, tracked_checkouts
):
    _dirty(conflicted.source)
    before = _snapshot(conflicted.source)

    def cancelled(checkout, conflicts):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        mc.resolve_merge_conflicts(
            conflicted.source, conflicted.head, conflicted.base, cancelled
        )

    assert _snapshot(conflicted.source) == before
    assert tracked_checkouts, "a disposable clone should have been created"
    assert all(not Path(path).exists() for path in tracked_checkouts)


def test_resolver_command_receives_conflict_payload(tmp_path):
    conflicts = [
        Conflict(
            path="a.txt",
            stage2=IndexStage("100644", "bbb"),
            stage3=IndexStage("100644", "ccc"),
        ),
        Conflict(path="sub", stage1=IndexStage("160000", "aaa")),
    ]
    out = tmp_path / "payload.json"
    script = "import sys; open({!r}, 'w').write(sys.stdin.read())".format(str(out))

    mc.run_resolver_command(["python3", "-c", script], tmp_path, conflicts)

    assert json.loads(out.read_text()) == [
        {
            "path": "a.txt",
            "stages": {
                "2": {"mode": "100644", "blob": "bbb"},
                "3": {"mode": "100644", "blob": "ccc"},
            },
        },
        {"path": "sub", "stages": {"1": {"mode": "160000", "blob": "aaa"}}},
    ]


def test_resolver_command_failure_raises(tmp_path):
    with pytest.raises(mc.MergeConflictResolutionError, match="Resolver failed"):
        mc.run_resolver_command(
            ["python3", "-c", "import sys; sys.exit(3)"], tmp_path, []
        )


def test_stop_group_kills_and_reaps_a_session():
    process = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    mc._stop_group(process)
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)
