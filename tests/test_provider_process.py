"""Provider descendants must stop before a checkpoint can touch the checkout."""

import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from ai_agent import provider_process


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    monkeypatch.setattr(
        provider_process, "active_project", lambda: SimpleNamespace(repo_path=tmp_path)
    )
    return tmp_path


def test_timeout_kills_descendant_writer_before_recovery(repo):
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c', "
        "\"import time,pathlib; time.sleep(1); pathlib.Path('late').write_text('bad')\"]); "
        "time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        provider_process.grouped_run(
            [sys.executable, "-c", script], cwd=repo, timeout=0.15, text=True
        )
    provider_process.require_no_writer()
    time.sleep(1.1)
    assert not (repo / "late").exists()


def test_success_kills_detached_stdio_descendant(repo):
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c', "
        "\"import time,pathlib; time.sleep(1); pathlib.Path('late').write_text('bad')\"],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    )
    result = provider_process.grouped_run(
        [sys.executable, "-c", script], cwd=repo, timeout=5, text=True
    )
    assert result.returncode == 0
    provider_process.require_no_writer()
    time.sleep(1.1)
    assert not (repo / "late").exists()


@pytest.mark.parametrize("value", [[], {"group": None}, {"group": "1"}])
def test_unknown_process_marker_fails_closed(repo, value):
    provider_process._marker().write_text(json.dumps(value))
    with pytest.raises(RuntimeError):
        provider_process.require_no_writer()


def test_explicit_repository_marker_is_used(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", str(other)], check=True, capture_output=True)
    assert provider_process._marker(other) == other / ".git/ai-agent-provider.json"


def test_grouped_provider_can_read_private_memory_attachment(repo):
    import os

    from ai_agent.image_delivery import codex_files

    if not hasattr(os, "memfd_create"):
        pytest.skip("Python interpreter does not expose Linux memfd_create")
    with codex_files([("image/png", b"private image bytes")]) as flags:
        result = provider_process.grouped_run(
            [
                sys.executable,
                "-c",
                "import sys; print(len(open(sys.argv[1], 'rb').read()))",
                flags[1],
            ],
            cwd=repo,
            timeout=5,
            text=True,
        )
    assert result.stdout.strip() == "19"
    provider_process.require_no_writer()
