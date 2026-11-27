"""Controller staging never follows an escaped path or reads unbounded files."""

import pytest

from ai_agent import merge_conflict_files as files
from ai_agent.merge_conflict_stages import Conflict


def test_replaced_parent_symlink_is_rejected_without_reading_target(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    parent = checkout / "nested"
    parent.mkdir()
    path = parent / "file"
    path.write_text("conflicted")
    before = {"nested/file": files.state(checkout, "nested/file")}
    path.unlink()
    parent.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("secret")
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink parent"):
        files.changed_paths(checkout, [Conflict("nested/file")], before)
    assert (outside / "file").read_text() == "secret"


def test_conflict_file_reads_are_bounded(tmp_path, monkeypatch):
    (tmp_path / "file").write_bytes(b"x" * 17)
    monkeypatch.setattr(files, "MAX_CONFLICT_BYTES", 16)
    with pytest.raises(ValueError, match="staging limit"):
        files.state(tmp_path, "file")
