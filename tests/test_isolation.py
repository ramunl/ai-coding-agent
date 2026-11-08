"""DS12 isolation: sandbox diff round-trips and root-gated escape checks.

The round-trip tests run without privileges: they build a real local Git
repository and a disposable worktree, mutate the worktree, then re-apply the
sandbox diff to the production checkout and assert nothing was lost. The
escape tests need root (the worker uid/gid must exist and the process must be
allowed to drop privileges), so they skip on a non-root CI runner and are the
checks Ops runs on the live host.
"""

import os
import subprocess

import pytest

from ai_agent import isolation


def _git(cwd, *args):
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL
    ).strip()


@pytest.fixture
def repository(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked").write_text("original\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def test_sandbox_name_is_stable_and_unique():
    first = isolation.sandbox_name("task-1", "feature/a")
    again = isolation.sandbox_name("task-1", "feature/a")
    other = isolation.sandbox_name("task-2", "feature/a")
    assert first == again
    assert first != other
    assert first.startswith("ai-run-")


def test_rename_round_trip(repository):
    sandbox = isolation.create_sandbox(repository, "task-rename", "feature/test")
    try:
        _git(sandbox.path, "mv", "tracked", "renamed")
        isolation.apply_sandbox(repository, sandbox)
    finally:
        isolation.remove_sandbox(repository, sandbox)
    assert (repository / "renamed").read_text() == "original\n"
    assert not (repository / "tracked").exists()


def test_binary_round_trip(repository):
    (repository / "blob.bin").write_bytes(bytes(range(256)))
    _git(repository, "add", "blob.bin")
    _git(repository, "commit", "-m", "binary")
    sandbox = isolation.create_sandbox(repository, "task-binary", "feature/test")
    try:
        (sandbox.path / "blob.bin").write_bytes(b"\x00\xff" * 128)
        isolation.apply_sandbox(repository, sandbox)
    finally:
        isolation.remove_sandbox(repository, sandbox)
    assert (repository / "blob.bin").read_bytes() == b"\x00\xff" * 128


def test_submodule_gitlink_round_trip(repository):
    child_source = repository.parent / "child-source"
    subprocess.run(
        ["git", "clone", "-q", str(repository), str(child_source)],
        check=True,
        capture_output=True,
    )
    _git(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(child_source),
        "child",
    )
    _git(repository, "commit", "-m", "add submodule")

    # A second clone stands in for the disposable provider checkout: a clone
    # checks the submodule out cleanly, which a linked worktree cannot, so this
    # isolates the diff round-trip (gitlink preservation) from worktree layout.
    sandbox = repository.parent / "sandbox"
    subprocess.run(
        ["git", "clone", "-q", str(repository), str(sandbox)],
        check=True,
        capture_output=True,
    )
    _git(
        sandbox,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
    )
    baseline = _git(sandbox, "rev-parse", "HEAD")

    child = sandbox / "child"
    (child / "extra").write_text("child work\n")
    _git(child, "add", ".")
    _git(
        child,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        "child work",
    )
    new_sha = _git(child, "rev-parse", "HEAD")
    _git(sandbox, "add", "child")

    isolation.apply_sandbox(
        repository, isolation.Sandbox(path=sandbox, baseline=baseline)
    )

    mode, recorded, *_ = _git(repository, "ls-files", "-s", "child").split()
    assert mode == "160000"
    assert recorded == new_sha


def _worker_ids():
    import grp
    import pwd

    try:
        uid = pwd.getpwnam(isolation.WORKER_USER).pw_uid
        gid = grp.getgrnam(isolation.WORKER_GROUP).gr_gid
    except KeyError:
        pytest.skip("worker user not provisioned on this host")
    return uid, gid


@pytest.mark.skipif(os.geteuid() != 0, reason="root-gated; Ops runs this on the host")
def test_worker_child_cannot_read_root_only_secrets(tmp_path):
    uid, gid = _worker_ids()
    secret = tmp_path / "secret"
    secret.write_text("owner secret\n")
    secret.chmod(0o600)
    result = subprocess.run(
        ["cat", str(secret)],
        user=uid,
        group=gid,
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"owner secret" not in result.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="root-gated; Ops runs this on the host")
def test_worker_child_cannot_write_outside_its_home(tmp_path):
    uid, gid = _worker_ids()
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    guarded.chmod(0o555)
    target = guarded / "escape"
    result = subprocess.run(
        ["touch", str(target)],
        user=uid,
        group=gid,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not target.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="root-gated; Ops runs this on the host")
def test_worker_child_can_write_inside_its_owned_dir(tmp_path):
    uid, gid = _worker_ids()
    home = tmp_path / "worker-home"
    home.mkdir()
    os.chown(home, uid, gid, follow_symlinks=False)
    result = subprocess.run(
        ["touch", str(home / "ok")],
        user=uid,
        group=gid,
        capture_output=True,
    )
    assert result.returncode == 0
    assert (home / "ok").exists()
