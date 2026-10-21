"""Recovery at the boundary between saving a task and starting it."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def actions(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("YOUR_CHAT_ID", "123")
    module = importlib.import_module("ai_agent.bot.task_actions")
    monkeypatch.setattr(module, "get_project", lambda name: SimpleNamespace(name=name))
    return module


@pytest.mark.parametrize("action", ["create_task", "create_autopilot_task"])
@pytest.mark.parametrize("unexpected", [False, True])
def test_startup_failure_keeps_one_queued_task(
    actions, monkeypatch, caplog, action, unexpected
):
    data = {}
    app = SimpleNamespace(user_data={123: data})
    images = [{"id": "screenshot.png"}]
    # Other tests reload config; patch the globals used by this imported helper.
    monkeypatch.setitem(
        actions.redact_sensitive.__globals__, "TELEGRAM_TOKEN", "startup-secret"
    )

    async def fail(app, owner, task):
        task["stage"] = "planning"
        if unexpected:
            raise RuntimeError("startup-secret unavailable")
        raise actions.ActionError("Not started: another request is being planned.")

    monkeypatch.setattr(actions, "start", fail)
    result = asyncio.run(
        actions.run_task_action(
            app,
            123,
            action,
            {"repo": "repo", "todo": "-", "text": "Fix layout", "images": images},
        )
    )
    (task,) = data["tasks"]
    assert task["stage"] == "todo"
    assert task["images"] == images
    assert result == f"Task added as To do: {task['note']}."
    assert bool(task.get("autopilot")) == (action == "create_autopilot_task")
    if unexpected:
        assert "[redacted] unavailable" in caplog.text
        assert "startup-secret" not in caplog.text + result
    else:
        assert task["note"] == "another request is being planned"


def test_startup_cancellation_propagates(actions, monkeypatch):
    data = {}
    monkeypatch.setattr(actions, "start", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            actions.run_task_action(
                SimpleNamespace(user_data={123: data}),
                123,
                "create_task",
                {"repo": "repo", "todo": "-", "text": "Fix layout"},
            )
        )
    assert len(data["tasks"]) == 1
