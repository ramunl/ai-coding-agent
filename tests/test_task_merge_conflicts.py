"""A task selects its own saved PR and repository for conflict resolution."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def wired(monkeypatch):
    module = importlib.import_module("ai_agent.bot.task_merge_conflicts")
    task = {
        "id": "abcd1234",
        "title": "Feature",
        "stage": "pr",
        "repo": "task-repo",
        "pr_url": "https://github.com/owner/task-repo/pull/108",
    }
    data = {"tasks": [task]}
    app = SimpleNamespace(user_data={123: data}, bot=object())
    context = SimpleNamespace(user_data=data)
    project = SimpleNamespace(name="task-repo", github_repository="owner/task-repo")
    monkeypatch.setattr(module, "get_project", lambda _: project)
    monkeypatch.setattr(module, "CallbackContext", lambda *a, **kw: context)
    monkeypatch.setattr(module.task_actions, "_switch_to", AsyncMock())
    monkeypatch.setattr(module.work_actions, "_starting", set())
    monkeypatch.setattr(
        module.work_actions,
        "chat_update",
        lambda *a: SimpleNamespace(message=SimpleNamespace(sent=[])),
    )
    monkeypatch.setattr(module, "fix_merge_conflicts", AsyncMock())

    async def start(handler, update, context):
        await handler(update, context)

    monkeypatch.setattr(module.work_actions, "_start", start)
    return module, app, task, data, context


def test_task_dispatch_uses_saved_pr_and_switches_to_its_repository(wired):
    module, app, task, data, context = wired
    result = asyncio.run(
        module.task_actions.run_task_action(
            app, 123, "fix_task_conflicts", {"task": task["id"]}
        )
    )
    module.task_actions._switch_to.assert_awaited_once_with(app, data, "task-repo")
    assert context.args == [task["pr_url"]]
    assert module.fix_merge_conflicts.await_args.kwargs == {"task_id": task["id"]}
    assert "Resolving" in result
    assert task["stage"] == "pr"


@pytest.mark.parametrize(
    "overrides",
    [
        {"stage": "done"},
        {"stage": "todo"},
        {"pr_url": None},
        {"pr_url": "https://example.com/pull/108"},
        {"pr_url": "https://github.com/owner/task-repo/issues/108"},
        {"pr_url": "javascript://github.com/owner/task-repo/pull/108"},
        {"pr_url": "https://github.com/owner/other-repo/pull/108"},
    ],
)
def test_ineligible_or_mismatched_task_never_switches_or_starts(wired, overrides):
    module, app, task, _, _ = wired
    task.update(overrides)
    with pytest.raises(module.ActionError):
        asyncio.run(module.run(app, 123, task))
    module.task_actions._switch_to.assert_not_awaited()
    module.fix_merge_conflicts.assert_not_awaited()


@pytest.mark.parametrize(
    "busy",
    [
        {"merge_conflict_active": True},
        {"active_execution": {"branch": "feature/other"}},
        {"pending_plan": object()},
        {"task_queue": [{}]},
    ],
)
def test_busy_agent_rejects_task_fix_before_switch(wired, busy):
    module, app, task, data, _ = wired
    data.update(busy)
    with pytest.raises(module.ActionError, match="Cannot resolve"):
        asyncio.run(module.run(app, 123, task))
    module.task_actions._switch_to.assert_not_awaited()
    module.fix_merge_conflicts.assert_not_awaited()


def test_task_removed_during_repository_switch_is_rejected(wired, monkeypatch):
    module, app, task, data, _ = wired

    async def remove(*_):
        data["tasks"].clear()

    monkeypatch.setattr(module.task_actions, "_switch_to", remove)
    with pytest.raises(module.ActionError, match="task changed"):
        asyncio.run(module.run(app, 123, task))
    module.fix_merge_conflicts.assert_not_awaited()


def test_unknown_task_is_rejected(wired):
    module, app, _, _, _ = wired
    with pytest.raises(module.ActionError, match="no longer exists"):
        asyncio.run(
            module.task_actions.run_task_action(
                app, 123, "fix_task_conflicts", {"task": "ffffffff"}
            )
        )


def test_snapshot_exposes_capability_only_for_pr_open_tasks(wired):
    module, _, task, data, _ = wired
    from ai_agent.tasks import view

    assert view(task, data)["can_fix_conflicts"] is True
    task["stage"] = "done"
    assert view(task, data)["can_fix_conflicts"] is False


def test_inbox_accepts_only_task_id_not_client_pr_or_repository(wired):
    from ai_agent.inbox import validate

    module, _, task, _, _ = wired
    request = {
        "id": "a" * 16,
        "action": "fix_task_conflicts",
        "args": {"task": task["id"]},
    }
    assert validate(request)[1] == "fix_task_conflicts"
    for key in ("url", "repo"):
        with pytest.raises(module.ActionError, match="expects exactly"):
            validate({**request, "args": {**request["args"], key: "untrusted"}})
