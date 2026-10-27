"""Reopening preserves history while isolating fresh attempts from old work."""

import asyncio
import importlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("YOUR_CHAT_ID", "123")
    return (
        importlib.import_module("ai_agent.tasks"),
        importlib.import_module("ai_agent.task_reopen"),
    )


def test_repeated_reopen_preserves_identity_history_and_dependencies(modules):
    tasks, reopen = modules
    data = {}
    task = tasks.add_task(data, "Fix layout", "repo", "todo:1", [{"id": "img"}])
    child = tasks.add_task(data, "Next part", "repo", None)
    child.update(after=task["id"], stage="done")
    original_child = deepcopy(child)
    identity = {
        key: task[key]
        for key in ("id", "text", "title", "images", "todo", "created_at")
    }
    task.update(
        stage="done",
        branch="feature/old",
        plan_id="plan1",
        pr_url="url",
        merged_at=4,
        merge_commit="sha",
        run={"codex_prompt": "private"},
        worker_failed=True,
        autopilot_stuck=True,
        autopilot=True,
    )
    reopen.reopen_task(task, data)
    first = deepcopy(task["attempt_history"])
    assert first[0]["branch"] == "feature/old"
    assert first[0]["merge_commit"] == "sha"
    assert first[0]["started_at"] == task["created_at"]
    assert first[0]["archived_at"] >= task["created_at"]
    assert {key: task[key] for key in identity} == identity
    assert child == original_child
    assert task["stage"] == "todo" and not task["autopilot"]
    assert all(key not in task for key in reopen.CLEAR_FIELDS)
    with pytest.raises(ValueError, match="Only Done or Stopped"):
        reopen.reopen_task(task, data)
    tasks.set_stage(task, "stopped", "cancelled")
    reopen.reopen_task(task, data)
    assert task["attempt"] == 3
    assert task["attempt_history"][:1] == first
    assert task["attempt_history"][1]["finished_at"] is not None
    assert not tasks.can_retry(task)


@pytest.mark.parametrize(
    "stage",
    [
        "todo",
        "planning",
        "planned",
        "implementing",
        "ended",
        "pr",
        "blocked",
        "ops_required",
        "proposed",
    ],
)
def test_ineligible_stages(modules, stage):
    tasks, reopen = modules
    task = {"id": "abcd1234", "stage": stage}
    assert not tasks.view(task, {})["can_reopen"]
    with pytest.raises(ValueError):
        reopen.reopen_task(task, {})


@pytest.mark.parametrize(
    "busy",
    [
        {"planning_task": "abcd1234"},
        {"task_queue": [{"branch_name": "old"}]},
        {"active_execution": {"branch": "old"}},
        {"pending_implementation": {"branch_name": "old"}},
        {"pending_plan": SimpleNamespace(id="plan")},
        {"worker": {"current": {"task": "abcd1234"}}},
    ],
)
def test_busy_attempt_refused_without_mutation(modules, busy):
    tasks, reopen = modules
    task = {"id": "abcd1234", "stage": "done", "branch": "old", "plan_id": "plan"}
    before = deepcopy(task)
    assert not tasks.view(task, busy)["can_reopen"]
    with pytest.raises(ValueError):
        reopen.reopen_task(task, busy)
    assert task == before


def test_persistence_and_safe_snapshot(modules, monkeypatch):
    tasks, reopen = modules
    persistence = importlib.import_module("ai_agent.bot.persistence")
    monkeypatch.setitem(reopen.redact_sensitive.__globals__, "TELEGRAM_TOKEN", "secret")
    data = {}
    task = tasks.add_task(data, "fix", "repo", None)
    task.update(stage="stopped", note="secret failed", run={"codex_prompt": "hidden"})
    reopen.reopen_task(task, data)
    saved = persistence.serialize_user_data(data)
    restored = persistence.deserialize_user_data(json.loads(json.dumps(saved)))
    assert restored["tasks"] == data["tasks"]
    restored["tasks"][0]["attempt_history"][0]["run"] = {"codex_prompt": "hidden"}
    snapshot = tasks.view(restored["tasks"][0], restored)
    text = json.dumps(snapshot)
    assert "secret" not in text and "hidden" not in text and "codex_prompt" not in text
    assert "[redacted] failed" in text


@pytest.mark.parametrize("change", ["attempt", "branch", "stage", "removed"])
def test_stale_poll_does_not_apply(modules, monkeypatch, change):
    tasks, _ = modules
    pulls = importlib.import_module("ai_agent.task_pulls")
    data = {}
    task = tasks.add_task(data, "fix", "repo", None)
    task.update(stage="pr", branch="old")
    monkeypatch.setattr(
        pulls, "get_project", lambda _: SimpleNamespace(github_repository="o/r")
    )

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def fake_thread(fn, *args):
            if fn is pulls.get_project:
                return fn(*args)
            entered.set()
            await release.wait()
            return {
                "state": "merged",
                "url": "old-pr",
                "merged_at": 9,
                "merge_commit": "sha",
            }

        monkeypatch.setattr(pulls.asyncio, "to_thread", fake_thread)
        poll = asyncio.create_task(pulls.check_once(data))
        await entered.wait()
        if change == "removed":
            data["tasks"].clear()
        else:
            task[change] = {"attempt": 2, "branch": "fresh", "stage": "todo"}[change]
        before = deepcopy(task)
        release.set()
        assert not await poll
        assert task == before

    asyncio.run(run())
