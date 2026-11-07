"""Bounded progress projection and the owner-only five-minute notifier."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ai_agent import task_progress


def _active(**overrides: object) -> dict:
    record = {
        "branch": "feature/a",
        "phase": "Running Codex",
        "status": "RUNNING",
        "stage": "implement",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "started_at": 0.0,
        "last_activity": 0.0,
    }
    record.update(overrides)
    return record


def _app(active: dict | None = None) -> SimpleNamespace:
    user_data = {42: {"active_execution": active}}
    bot = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(user_data=user_data, bot=bot)


def test_progress_projects_canonical_stage_and_ages() -> None:
    view = task_progress.progress(
        _active(started_at=1000.0, last_activity=1010.0), now=1100.0
    )
    assert view == {
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "stage": "implement",
        "elapsed": 100,
        "last_activity": 90,
    }


def test_progress_is_none_when_idle() -> None:
    assert task_progress.progress(None, now=1100.0) is None


def test_progress_rejects_paths_prose_and_bad_timestamps() -> None:
    active = _active(
        task_id="../secret/path",
        attempt_id="attempt id with spaces",
        started_at=-1,
        last_activity="not a timestamp",
    )
    view = task_progress.progress(active, now=1100.0)
    assert view["task_id"] is None
    assert view["attempt_id"] is None
    assert view["elapsed"] is None
    assert view["last_activity"] is None
    assert view["stage"] == "implement"
    assert "secret" not in json.dumps(view)


def test_stage_label_maps_phases_and_defaults_to_working() -> None:
    assert task_progress.stage_label("Checking GitHub configuration") == "checkout"
    assert task_progress.stage_label("Running Codex") == "implement"
    assert task_progress.stage_label("Committing and pushing branch") == "push"
    assert task_progress.stage_label("Opening GitHub PR") == "pr"
    assert task_progress.stage_label("Polling CI") == "ci"
    assert task_progress.stage_label("Repairing CI failure (attempt 1/3)") == "repair"
    assert task_progress.stage_label("Stopping; preserving saved work") == "stopping"
    assert task_progress.stage_label("something unexpected") == "working"


def test_restart_progress_is_an_unknown_marker() -> None:
    assert task_progress.restart_progress() == {
        "task_id": None,
        "attempt_id": None,
        "stage": "unknown",
        "elapsed": None,
        "last_activity": None,
    }


def test_journal_progress_marks_unfinished_runs_unknown() -> None:
    state = {
        "spec": {"task_id": "task-1"},
        "attempt_id": "attempt-1",
        "started_at": 1000.0,
        "events": {"k": {"stage": "ci", "observed_at": 1050.0}},
        "latest": {"ci": "k"},
        "terminal": None,
    }
    view = task_progress.journal_progress(state, now=1100.0)
    assert view == {
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "stage": "unknown",
        "elapsed": 100,
        "last_activity": 50,
    }


def test_journal_progress_terminal_and_legacy_events() -> None:
    terminal = {"spec": {"task_id": "t"}, "attempt_id": "a", "events": {}}
    assert task_progress.journal_progress(terminal | {"terminal": "succeeded"}) is None
    legacy = {
        "spec": {"task_id": "task-1"},
        "attempt_id": "attempt-1",
        "started_at": 1000.0,
        "events": {"k": {"stage": "ci", "facts": {}}},
        "latest": {"ci": "k"},
        "terminal": None,
    }
    view = task_progress.journal_progress(legacy, now=1100.0)
    assert view["last_activity"] is None


def test_notify_once_five_minute_cadence_and_quiet_note() -> None:
    task_progress.clear_progress_state()
    app = _app(_active())
    assert asyncio.run(task_progress.notify_once(app, 42, now=0.0)) == 1
    assert app.bot.send_message.await_count == 1
    first = app.bot.send_message.await_args.kwargs["text"]
    assert "(no new activity)" not in first

    # Inside the five-minute window nothing else is sent.
    assert asyncio.run(task_progress.notify_once(app, 42, now=200.0)) == 0
    assert app.bot.send_message.await_count == 1

    # A due tick with unchanged last-activity notes it instead of repeating.
    assert asyncio.run(task_progress.notify_once(app, 42, now=300.0)) == 1
    assert app.bot.send_message.await_count == 2
    second = app.bot.send_message.await_args.kwargs["text"]
    assert "(no new activity)" in second


def test_notify_once_reports_stage_changes() -> None:
    task_progress.clear_progress_state()
    app = _app(_active())
    asyncio.run(task_progress.notify_once(app, 42, now=0.0))

    active = app.user_data[42]["active_execution"]
    active["stage"] = "pr"
    active["phase"] = "Opening GitHub PR"
    active["last_activity"] = 290.0

    assert asyncio.run(task_progress.notify_once(app, 42, now=300.0)) == 1
    text = app.bot.send_message.await_args.kwargs["text"]
    assert "Progress: pr" in text
    assert "(no new activity)" not in text


def test_notifier_never_calls_a_provider(monkeypatch) -> None:
    def unexpected(*args, **kwargs):
        raise AssertionError("the notifier must not offload AI work")

    monkeypatch.setattr(asyncio, "to_thread", unexpected)
    task_progress.clear_progress_state()
    app = _app(_active())
    assert asyncio.run(task_progress.notify_once(app, 42, now=0.0)) == 1
    assert app.bot.send_message.await_count == 1
    assert app.bot.send_message.await_args.kwargs["chat_id"] == 42


def test_notify_once_is_quiet_after_a_run_ends() -> None:
    task_progress.clear_progress_state()
    app = _app(_active())
    assert asyncio.run(task_progress.notify_once(app, 42, now=0.0)) == 1

    # The execution flow pops active_execution on every terminal path.
    app.user_data[42].pop("active_execution")
    assert asyncio.run(task_progress.notify_once(app, 42, now=300.0)) == 0
    assert app.bot.send_message.await_count == 1
    assert task_progress._last_update == {}


def test_stale_attempt_slot_does_not_suppress_a_new_run() -> None:
    task_progress.clear_progress_state()
    old = _app(_active(attempt_id="attempt-old"))
    asyncio.run(task_progress.notify_once(old, 42, now=0.0))

    new = _app(_active(attempt_id="attempt-new", last_activity=290.0))
    assert asyncio.run(task_progress.notify_once(new, 42, now=300.0)) == 1
    assert new.bot.send_message.await_count == 1
