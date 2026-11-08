"""Code upgrades must not silently activate an unprovisioned identity migration."""

from pathlib import Path
from unittest.mock import patch

import pytest

from ai_agent import config, isolation, workflow
from ai_agent.shell import CommandResult


def test_default_unit_keeps_existing_controller_identity_and_home():
    unit = (
        Path(__file__).resolve().parents[1] / "deploy/ai-coding-agent.service"
    ).read_text()
    assert "User=ai-coding-runner" not in unit
    assert "Group=ai-coding-runner" not in unit
    assert "Environment=HOME=/root" in unit
    assert "ProtectHome=yes" not in unit  # Existing CLI/auth paths are under /root.


@pytest.mark.parametrize("enabled", [False, True])
def test_worker_privilege_drop_requires_explicit_activation(monkeypatch, enabled):
    monkeypatch.setattr(workflow, "WORKER_ISOLATION_ENABLED", enabled)
    monkeypatch.setattr(isolation, "current_sandbox", lambda: None)
    monkeypatch.setattr(workflow, "record_detail", lambda *a: None)
    monkeypatch.setattr(workflow, "record_usage", lambda *a: None)
    monkeypatch.setattr(workflow, "preflight", lambda agent: None)
    with patch.object(
        workflow, "run_with_images", return_value=CommandResult([], 0, "ok")
    ) as run:
        workflow.run_implementation_agent("fix", "codex")
    assert run.call_args.kwargs["isolated"] is enabled
    assert run.call_args.kwargs["provider_run"] is True


def test_compatibility_mode_does_not_create_or_chown_worker_home(monkeypatch, tmp_path):
    monkeypatch.setattr(isolation, "WORKER_ISOLATION_ENABLED", False)
    home = tmp_path / "missing-home"
    monkeypatch.setattr(isolation, "WORKER_HOME", home)
    with patch.object(
        isolation, "_uid", side_effect=AssertionError("No account lookup")
    ):
        isolation._prepare_ownership(tmp_path)
    assert not home.exists()


def test_enabled_worker_migration_fails_closed_if_account_missing(
    monkeypatch, tmp_path
):
    import pwd

    monkeypatch.setattr(config, "WORKER_ISOLATION_ENABLED", True)
    monkeypatch.setattr(config, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(config, "WORKER_HOME", tmp_path / "home")
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: (_ for _ in ()).throw(KeyError(name))
    )
    with pytest.raises(RuntimeError, match="complete the Ops migration"):
        config.validate_work_config()
