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


def test_reopen_dispatch_and_duplicate_feedback(actions):
    task = {"id": "abcd1234", "title": "Fix", "stage": "done"}
    data = {"tasks": [task]}
    app = SimpleNamespace(user_data={123: data})
    result = asyncio.run(
        actions.run_task_action(app, 123, "reopen_task", {"task": task["id"]})
    )
    assert "To do" in result and "approval" in result
    with pytest.raises(actions.ActionError, match="Only Done or Stopped"):
        asyncio.run(
            actions.run_task_action(app, 123, "reopen_task", {"task": task["id"]})
        )
    with pytest.raises(actions.ActionError, match="no longer exists"):
        asyncio.run(
            actions.run_task_action(app, 123, "reopen_task", {"task": "ffffffff"})
        )


@pytest.mark.parametrize("planner", [None, "codex"])
def test_manual_and_worker_replan_fresh_branch(actions, monkeypatch, planner):
    from ai_agent.plan_state import new_plan_state, parse_plan_document
    from ai_agent.task_reopen import reopen_task
    from ai_agent.tasks import add_task

    data = {}
    task = add_task(data, "Fix layout", "repo", None)
    task.update(stage="done", branch="feature/old", pr_url="old-pr")
    reopen_task(task, data)
    app = SimpleNamespace(user_data={123: data}, bot=object())
    context = SimpleNamespace(user_data=data)
    monkeypatch.setattr(actions, "CallbackContext", lambda *a, **kw: context)
    monkeypatch.setattr(actions, "_switch_to", AsyncMock())
    monkeypatch.setattr(actions.work_actions, "_starting", set())
    monkeypatch.setattr(
        actions.work_actions, "chat_update", lambda *a: SimpleNamespace(message=None)
    )
    monkeypatch.setattr(
        actions.work_actions, "summary", lambda message, fallback: fallback
    )

    async def plan(update, ctx):
        assert "old-pr" in " ".join(ctx.args)
        assert "fresh planning" in " ".join(ctx.args)
        data["pending_plan"] = new_plan_state(
            "Fix layout", '{"branch":"feature/old","summary":"fix"}'
        )

    async def start(handler, update, ctx):
        job = asyncio.create_task(handler(update, ctx))
        await job
        return job

    monkeypatch.setattr(actions, "plan", plan)
    monkeypatch.setattr(actions.work_actions, "_start", start)
    asyncio.run(actions.start(app, 123, task, planner))
    pending = data["pending_plan"]
    assert not pending.approved
    assert task["stage"] == "planned"
    assert task["branch"] == parse_plan_document(pending.plan_text).branch
    assert task["branch"] == f"feature/reopen-{task['id']}-2"
    assert not task["autopilot"]


def test_branch_is_displayed_and_preserved_through_revision_and_approval(
    actions, monkeypatch
):
    import contextlib

    from ai_agent.bot import planning
    from ai_agent.plan_state import new_plan_state, parse_plan_document
    from ai_agent.task_reopen import enforce_attempt_branch

    task = {"id": "abcd1234", "attempt": 2, "stage": "planned"}
    pending = new_plan_state("fix", '{"summary":"fix", "branch":"feature/old"}')
    enforce_attempt_branch(task, pending)
    task["plan_id"] = pending.id
    task["branch"] = parse_plan_document(pending.plan_text).branch
    data = {"tasks": [task], "pending_plan": pending}
    context = SimpleNamespace(user_data=data, args=["revise"])
    monkeypatch.setattr(planning, "require_authorized", lambda _: True)
    monkeypatch.setattr(planning, "repository_mismatch", lambda _: None)
    monkeypatch.setattr(planning, "_not_here", lambda _: None)
    monkeypatch.setattr(planning, "planning_guard", lambda _: contextlib.nullcontext())
    monkeypatch.setattr(planning, "current_planning_agent", lambda _: "codex")
    monkeypatch.setattr(planning, "report_missing", AsyncMock())
    monkeypatch.setattr(planning, "forget_pending_implementation", lambda _: None)
    monkeypatch.setattr(
        planning,
        "think",
        AsyncMock(return_value='{"branch":"feature/old", "summary":"revised"}'),
    )
    replies = AsyncMock()
    monkeypatch.setattr(planning, "reply_chunks", replies)
    monkeypatch.setattr(
        planning,
        "set_pending_from_plan",
        lambda ctx, plan: data.update(
            pending_implementation={
                "branch_name": parse_plan_document(plan.plan_text).branch
            }
        ),
    )
    asyncio.run(planning.discuss(SimpleNamespace(), context))
    revised = data["pending_plan"]
    assert not revised.approved
    assert task["branch"] == parse_plan_document(revised.plan_text).branch
    assert task["branch"] in replies.call_args.args[1]
    asyncio.run(planning.approve(SimpleNamespace(), context))
    assert data["pending_implementation"]["branch_name"] == task["branch"]
