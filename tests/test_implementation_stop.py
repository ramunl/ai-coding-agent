"""Owner cancellation uses the existing cleanup and checkpoint boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("restored", [True, False])
def test_stop_status_waits_for_cleanup_and_does_not_publish(monkeypatch, restored):
    from ai_agent.bot import execution
    from ai_agent.implementation_stop import ImplementationStoppedError

    task = {
        "id": "abc12345",
        "stage": "implementing",
        "repo": "repo",
        "branch": "feature/test",
        "stop_requested": True,
    }
    data = {"tasks": [task], "active_execution": {"branch": "feature/test"}}
    context = SimpleNamespace(user_data=data)
    queued = {
        "checkpoint_task": task["id"],
        "branch_name": task["branch"],
        "repository_identity": {"project_name": "repo"},
        "implementation_agent": "claude",
    }
    monkeypatch.setattr(execution, "repository_mismatch", lambda identity: None)
    monkeypatch.setattr(execution, "bind_checkpoint", lambda *args: None)
    monkeypatch.setattr(
        execution,
        "_publish_queued_implementation",
        AsyncMock(side_effect=ImplementationStoppedError("stopped")),
    )

    async def cleanup(update):
        assert task["stage"] == "implementing"
        assert "Stopping" in data["active_execution"]["phase"]
        return restored

    monkeypatch.setattr(execution, "reset_to_base_branch", cleanup)
    function = execution.run_queued_implementation.__wrapped__.__wrapped__
    with pytest.raises(ImplementationStoppedError):
        asyncio.run(function(None, context, queued))
    assert (task["stage"] == "stopped") is restored
    assert "active_execution" not in data
    assert "stop_requested" not in task
    if restored:
        assert task["autopilot"] is False
        assert task["worker_failed"] is True
