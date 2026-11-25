"""Owner cancellation uses the existing cleanup and checkpoint boundaries."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("restored", [True, False])
def test_stop_status_waits_for_cleanup_and_does_not_publish(
    monkeypatch, restored, direct
):
    from ai_agent.bot import execution
    from ai_agent.implementation_stop import ImplementationStoppedError

    task = {
        "id": "abc12345",
        "stage": "implementing",
        "repo": "repo",
        "branch": "feature/test",
        "stop_requested": True,
    }
    data = {
        "tasks": [] if direct else [task],
        "active_execution": {"branch": "feature/test"},
    }
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
    assert (task["stage"] == "stopped") is (restored and not direct)
    assert "active_execution" not in data
    if not direct:
        assert "stop_requested" not in task
    if restored and not direct:
        assert task["autopilot"] is False
        assert task["worker_failed"] is True


@pytest.mark.parametrize("task_id", ["abc12345", ""])
def test_current_capability_stale_duplicate_and_task_compatibility(task_id):
    from ai_agent import implementation_stop as stop

    active = {"branch": "feature/test"}
    old = stop.register(task_id, active["branch"])
    stop.unregister(old)
    run = stop.register(task_id, active["branch"])
    try:
        assert stop.current_status(active)["stop_token"] == run.token
        for state, token in [
            (active, old.token),
            (None, run.token),
            ({"branch": "other"}, run.token),
        ]:
            with pytest.raises(ValueError):
                stop.request_current(state, token)
        assert not run.stop.is_set()
        assert stop.request_current(active, run.token) == task_id
        assert stop.request_current(active, run.token) == task_id
        assert stop.status(task_id, active["branch"])["stopping"]
        stop.request(task_id, active["branch"], run.token)
        assert stop.unregister(run) is True
        with pytest.raises(ValueError):
            stop.request_current(active, run.token)
    finally:
        stop.unregister(run)


def test_queue_pauses_after_stop_and_can_resume(monkeypatch):
    from ai_agent.bot import execution
    from ai_agent.implementation_stop import ImplementationStoppedError

    first, second = {"id": 1}, {"id": 2}
    context = SimpleNamespace(user_data={"task_queue": [first, second]})
    monkeypatch.setattr(execution, "recovery_refusal", lambda data: None)
    monkeypatch.setattr(execution, "repository_mismatch", lambda identity: None)
    monkeypatch.setattr(execution, "require_run", lambda task: None)
    run = AsyncMock(side_effect=ImplementationStoppedError("stopped"))
    monkeypatch.setattr(execution, "run_queued_implementation", run)
    with pytest.raises(ImplementationStoppedError):
        asyncio.run(execution.run_task_queue(None, context))
    assert context.user_data["task_queue"] == [second]
    assert not context.user_data.get("queue_runner_active")
    run.side_effect = None
    asyncio.run(execution.run_task_queue(None, context))
    assert run.call_args.args[2] is second
    assert context.user_data["task_queue"] == []


@pytest.mark.parametrize(
    "phase",
    [
        "Planning",
        "Committing and pushing branch",
        "Polling CI",
        "Stopping; preserving saved work",
    ],
)
def test_snapshot_unavailable_phases_and_recovery(phase):
    from ai_agent.bot.snapshot_publisher import build_content

    content = build_content(
        {"active_execution": {"branch": "x", "phase": phase}}, {}, {}
    )
    capability = content["implementation_stop"]
    assert not capability["can_stop"]
    assert "stop_token" not in capability
    assert capability["stopping"] is phase.startswith("Stopping")
    assert content["running"] == {
        "branch": "x",
        "phase": phase,
        "can_stop": False,
        "stopping": phase.startswith("Stopping"),
    }


def test_snapshot_live_direct_capability_preserves_queue():
    from ai_agent import implementation_stop as stop
    from ai_agent.bot.snapshot_publisher import build_content

    run = stop.register("", "feature/direct")
    data = {
        "active_execution": {"branch": run.branch},
        "task_queue": [{"id": 2, "branch_name": "feature/queued"}],
        "queue_runner_active": True,
    }
    try:
        content = build_content(data, {}, {})
        assert content["implementation_stop"]["stop_token"] == run.token
        assert content["queue"][0]["branch"] == "feature/queued"
        assert not content["queue_paused"]
        stop.request_current(data["active_execution"], run.token)
        assert build_content(data, {}, {})["implementation_stop"]["stopping"]
    finally:
        stop.unregister(run)
    data.pop("queue_runner_active")
    data.pop("active_execution")
    content = build_content(data, {}, {})
    assert content["queue_paused"]
    assert not content["implementation_stop"]["can_stop"]


def test_coding_stop_preserves_task_stop_bookkeeping():
    from ai_agent import implementation_stop as stop
    from ai_agent.bot.work_actions import run_work_action

    task = {"id": "abc12345", "autopilot": True}
    data = {"tasks": [task], "active_execution": {"branch": "feature/task"}}
    app = SimpleNamespace(
        user_data={42: data}, mark_data_for_update_persistence=MagicMock()
    )
    run = stop.register(task["id"], "feature/task")
    try:
        asyncio.run(run_work_action(app, 42, "stop_work", {"token": run.token}))
        assert task["stop_requested"]
        assert task["worker_failed"]
        assert not task["autopilot"]
        app.mark_data_for_update_persistence.assert_called_once_with(user_ids=42)
    finally:
        stop.unregister(run)


def test_unbound_planning_session_has_no_stop_capability():
    from ai_agent import implementation_stop as stop

    assert stop.register("", "") is None
    assert not stop.current_status(None)["can_stop"]


@pytest.mark.parametrize("task_id", ["abc12345", ""])
def test_snapshot_running_token_lifetime_and_stop_work(task_id):
    from ai_agent import implementation_stop as stop
    from ai_agent.actions import ActionError
    from ai_agent.bot.snapshot_publisher import build_content
    from ai_agent.bot.work_actions import run_work_action

    data = {
        "active_execution": {"branch": "feature/lifetime", "phase": "Implementing"},
        "tasks": [{"id": task_id, "images": []}] if task_id else [],
        "task_queue": [],
    }
    original = deepcopy(data)
    app = SimpleNamespace(
        user_data={42: data}, mark_data_for_update_persistence=MagicMock()
    )
    run = stop.register(task_id, data["active_execution"]["branch"])
    try:
        content = build_content(data, {}, {})
        running = content["running"]
        assert running == {
            **data["active_execution"],
            "can_stop": True,
            "stopping": False,
            "stop_token": run.token,
        }
        assert content["implementation_stop"] == {
            "can_stop": True,
            "stopping": False,
            "stop_token": running["stop_token"],
            "queue_policy": "pause",
        }
        assert data == original
        assert running is not data["active_execution"]
        args = {"token": running["stop_token"]}
        asyncio.run(run_work_action(app, 42, "stop_work", args))
        assert run.stop.is_set()
        assert build_content(data, {}, {})["running"]["stopping"]
    finally:
        stop.unregister(run)

    data["active_execution"]["phase"] = "Stopping; preserving saved work"
    recovery = build_content(data, {}, {})["running"]
    assert recovery["stopping"] and not recovery["can_stop"]
    assert "stop_token" not in recovery
    with pytest.raises(ActionError):
        asyncio.run(run_work_action(app, 42, "stop_work", args))

    data["active_execution"]["phase"] = "Implementing"
    replacement = stop.register(task_id, run.branch)
    try:
        fresh = build_content(data, {}, {})["running"]
        assert fresh["stop_token"] != running["stop_token"]
        with pytest.raises(ActionError):
            asyncio.run(run_work_action(app, 42, "stop_work", args))
        assert not replacement.stop.is_set()
        asyncio.run(
            run_work_action(app, 42, "stop_work", {"token": fresh["stop_token"]})
        )
        assert replacement.stop.is_set()
    finally:
        stop.unregister(replacement)
    retired = build_content(data, {}, {})["running"]
    assert not retired["can_stop"] and not retired["stopping"]
    assert "stop_token" not in retired


def test_snapshot_idle_keeps_running_null():
    from ai_agent.bot.snapshot_publisher import build_content

    content = build_content({}, {}, {})
    assert content["running"] is None
    assert not content["implementation_stop"]["can_stop"]
    assert "stop_token" not in content["implementation_stop"]


def test_snapshot_stop_views_share_one_status_read(monkeypatch):
    from ai_agent.bot import snapshot_publisher

    active = {"branch": "feature/test"}
    status = MagicMock(
        return_value={"can_stop": False, "stopping": True, "queue_policy": "pause"}
    )
    monkeypatch.setattr(snapshot_publisher, "current_status", status)
    content = snapshot_publisher.build_content({"active_execution": active}, {}, {})
    status.assert_called_once_with(active)
    assert content["running"]["can_stop"] == content["implementation_stop"]["can_stop"]
    assert content["running"]["stopping"] == content["implementation_stop"]["stopping"]
