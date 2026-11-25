"""The Coding snapshot grants only live, attempt-bound inbox authority."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ai_agent import implementation_stop as stop


@pytest.mark.parametrize("task_id", ["", "abc12345"])
def test_snapshot_to_inbox_stop_is_cooperative_and_repeatable(task_id):
    from ai_agent.actions import ActionError
    from ai_agent.bot.snapshot_publisher import build_content
    from ai_agent.bot.work_actions import run_work_action
    from ai_agent.inbox import execute, validate

    task = {"id": task_id, "autopilot": True}
    data = {
        "active_execution": {"branch": "feature/run", "phase": "Running Codex"},
        "tasks": [task] if task_id else [],
        "task_queue": [{"id": 2}],
    }
    app = SimpleNamespace(
        user_data={42: data}, mark_data_for_update_persistence=MagicMock()
    )
    run = stop.register(task_id, "feature/run")
    try:
        running = build_content(data, {}, {})["running"]
        assert running["can_stop"] and not running["stopping"]
        _, action, args = validate(
            {
                "id": "12345678",
                "action": "stop_run",
                "args": {"token": running["stop_token"]},
            }
        )

        async def dispatch():
            execution = asyncio.current_task()
            for _ in range(2):
                await execute(
                    action, args, data, work=lambda a, b: run_work_action(app, 42, a, b)
                )
                assert not execution.cancelling()

        asyncio.run(dispatch())
        assert build_content(data, {}, {})["running"]["stopping"]
        assert data["task_queue"] == [{"id": 2}]
        if task_id:
            assert task["stop_requested"] and task["worker_failed"]
            assert not task["autopilot"]
        stop.unregister(run)
        with pytest.raises(ActionError):
            asyncio.run(run_work_action(app, 42, action, args))
    finally:
        stop.unregister(run)


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"token": None},
        {"token": 1},
        {"token": "f" * 31},
        {"token": "F" * 32},
        {"token": "f" * 32 + "\n"},
        {"token": "f" * 32, "task": "abc12345"},
    ],
)
def test_stop_run_strict_arguments(args):
    from ai_agent.actions import ActionError
    from ai_agent.inbox import validate

    with pytest.raises(ActionError):
        validate({"id": "12345678", "action": "stop_run", "args": args})


def test_restart_cannot_restore_stopping_or_capability():
    from ai_agent.bot.snapshot_publisher import build_content

    running = build_content(
        {
            "active_execution": {
                "branch": "old",
                "phase": "Stopping; preserving saved work",
                "can_stop": True,
                "stopping": True,
                "stop_token": "f" * 32,
            }
        },
        {},
        {},
    )["running"]
    assert not running["can_stop"]
    assert not running["stopping"]
    assert "stop_token" not in running


@pytest.mark.parametrize(
    "phase", ["Planning", "Polling CI", "Committing and pushing branch", ""]
)
def test_nonimplementation_display_cannot_stop_live_provider(phase):
    run = stop.register("", "feature/run")
    try:
        active = {"branch": run.branch, "phase": phase}
        assert not stop.current_status(active)["can_stop"]
        with pytest.raises(ValueError):
            stop.request_current(active, run.token)
        assert not run.stop.is_set()
    finally:
        stop.unregister(run)


def test_provider_retirement_revokes_token_before_descendant_cleanup():
    active = {"branch": "feature/run", "phase": "Running Codex"}
    run = stop.register("", active["branch"])
    try:
        stop.request_current(active, run.token)
        assert stop.retire(run)
        capability = stop.current_status(active)
        assert capability["stopping"] and not capability["can_stop"]
        assert "stop_token" not in capability
        with pytest.raises(ValueError):
            stop.request_current(active, run.token)
        with pytest.raises(ValueError):
            stop.request("", run.branch, run.token)
    finally:
        stop.unregister(run)


def test_same_branch_replacement_and_different_displayed_attempt():
    active = {"branch": "feature/run", "phase": "Running Codex", "attempt_id": "first"}
    first = stop.register("", active["branch"], "first")
    stop.unregister(first)
    second = stop.register("", active["branch"], "second")
    try:
        assert not stop.current_status(active)["can_stop"]
        with pytest.raises(ValueError):
            stop.request_current(active, second.token)
        active["attempt_id"] = "second"
        assert stop.current_status(active)["stop_token"] == second.token
        with pytest.raises(ValueError):
            stop.request_current(active, first.token)
        assert not second.stop.is_set()
        # A late finalizer for the previous provider cannot retire its replacement.
        stop.unregister(first)
        stop.request_current(active, second.token)
        assert second.stop.is_set()
    finally:
        stop.unregister(second)


def test_cleanup_failure_cannot_leave_live_authority_or_restored_stopping():
    active = {"branch": "feature/run", "phase": "Stopping; preserving saved work"}
    with pytest.raises(RuntimeError, match="cleanup failed"):
        with stop.stopping_cleanup(active):
            assert stop.current_status(active)["stopping"]
            assert not stop.current_status(dict(active))["stopping"]
            assert not stop.current_status(active)["can_stop"]
            raise RuntimeError("cleanup failed")
    assert not stop.current_status(active)["stopping"]


@pytest.mark.parametrize("stop_first", [True, False])
def test_provider_exit_race_has_one_atomic_winner(stop_first):
    active = {"branch": "feature/run", "phase": "Running Codex"}
    run = stop.register("", active["branch"])
    try:
        if stop_first:
            stop.request_current(active, run.token)
            assert stop.retire(run)
        else:
            assert not stop.retire(run)
            with pytest.raises(ValueError):
                stop.request_current(active, run.token)
            assert not run.stop.is_set()
    finally:
        stop.unregister(run)


@pytest.mark.parametrize(
    "action,args",
    [
        ("start_work", {"kind": "implement", "text": "fix"}),
        ("confirm_work", {}),
        ("remove_queued", {"task": "2"}),
    ],
)
def test_existing_work_actions_still_validate_and_dispatch(action, args):
    from unittest.mock import AsyncMock

    from ai_agent.inbox import execute, validate

    _, validated, arguments = validate(
        {"id": "12345678", "action": action, "args": args}
    )
    work = AsyncMock(return_value="accepted")
    assert asyncio.run(execute(validated, arguments, {}, work=work)) == "accepted"
    work.assert_awaited_once_with(action, args)


@pytest.mark.parametrize("replace", [False, True])
def test_snapshot_token_through_file_inbox(tmp_path, replace):
    import json

    from ai_agent.bot.snapshot_publisher import build_content
    from ai_agent.bot.work_actions import run_work_action
    from ai_agent.inbox import ResultLog, process_inbox

    data = {"active_execution": {"branch": "feature/direct", "phase": "Running Codex"}}
    app = SimpleNamespace(
        user_data={42: data}, mark_data_for_update_persistence=MagicMock()
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    log = ResultLog(tmp_path / "results.json")
    run = stop.register("", "feature/direct")
    try:
        token = build_content(data, {}, {})["running"]["stop_token"]
        if replace:
            stop.unregister(run)
            run = stop.register("", "feature/direct")
        (inbox / "12345678.json").write_text(
            json.dumps(
                {
                    "id": "12345678",
                    "action": "stop_run",
                    "args": {"token": token},
                    "requested_at": 1000,
                }
            )
        )
        count = asyncio.run(
            process_inbox(
                inbox,
                log,
                data,
                now=1001,
                work=lambda a, b: run_work_action(app, 42, a, b),
            )
        )
        assert count == 1
        assert run.stop.is_set() is (not replace)
        assert log.results[0]["status"] == ("failed" if replace else "done")
        assert not list(inbox.glob("*.json"))
    finally:
        stop.unregister(run)
