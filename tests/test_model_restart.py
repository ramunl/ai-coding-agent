"""Model restart targets and truthful scheduler failures."""

import importlib
import subprocess
from unittest.mock import patch

import pytest

from ai_agent import self_update


def test_default_restart_targets_coding_service(monkeypatch):
    """A successful save must restart the actual coding unit."""
    monkeypatch.delenv("AGENT_SERVICE_NAME", raising=False)
    importlib.reload(self_update)
    with patch.object(self_update.subprocess, "run") as run:
        self_update.schedule_restart()
    args = run.call_args.args[0]
    assert args[-1].endswith("systemctl restart ai-coding-agent")
    assert any(arg.startswith("--unit=ai-coding-agent-selfupdate-") for arg in args)


def test_restart_scheduler_failure_is_not_reported_as_success():
    """A shell in the bot's own cgroup cannot safely replace systemd-run."""
    with patch.object(
        self_update.subprocess,
        "run",
        side_effect=subprocess.CalledProcessError(1, "systemd-run"),
    ):
        with pytest.raises(RuntimeError, match="restart could not be scheduled"):
            self_update.schedule_restart()
