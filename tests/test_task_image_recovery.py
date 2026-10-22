"""Startup image recovery, including quarantine and subsequent state saves."""

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_agent import task_images
from ai_agent.bot.persistence import JsonStatePersistence, read_state_result
from tests.bot_fixtures import TelegramTestCase


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    case = TelegramTestCase()
    case.setUp()
    telegram_bot = importlib.import_module("ai_agent.telegram_bot")
    monkeypatch.setattr(task_images, "IMAGE_DIR", tmp_path / "images")
    monkeypatch.setattr(task_images, "IMAGE_STORAGE_CAP_BYTES", 100)
    monkeypatch.setattr(task_images, "decode", lambda _: ("png", 1, 1))
    monkeypatch.setattr(telegram_bot, "initialize", task_images.initialize)
    monkeypatch.setattr(telegram_bot, "_notify_core_drift_on_startup", AsyncMock())
    monkeypatch.setattr(telegram_bot, "_notify_restored_queue", AsyncMock())
    monkeypatch.setattr(telegram_bot, "_start_snapshot_publisher", lambda _: None)
    try:
        yield tmp_path / "state.json"
    finally:
        case.tearDown()


def startup(path):
    telegram_bot = importlib.import_module("ai_agent.telegram_bot")
    persistence = JsonStatePersistence(path)
    data = asyncio.run(persistence.get_user_data())
    app = SimpleNamespace(
        persistence=persistence,
        user_data=data,
        bot=SimpleNamespace(set_my_commands=AsyncMock()),
    )
    asyncio.run(telegram_bot.configure_bot_commands(app))
    return persistence


@pytest.mark.parametrize("failure", ["read_error", "malformed_json", "invalid_schema"])
def test_startup_preserves_bytes_after_failure_quarantine_and_saves(
    recovery, monkeypatch, failure
):
    refs = [task_images.save(b"first"), task_images.save(b"second")]
    original = '{"users": []}' if failure == "invalid_schema" else "{ broken"
    recovery.write_text(original)
    (task_images.IMAGE_DIR / "interrupted.part").write_bytes(b"temporary")
    read_text = Path.read_text

    def read(path, *args, **kwargs):
        if path == recovery and failure == "read_error":
            raise PermissionError("denied")
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read)
        persistence = startup(recovery)
    assert persistence.read_result.status == failure
    assert not persistence.read_result.references_known
    assert recovery.with_name("state.json.corrupt").read_text() == original
    assert not list(task_images.IMAGE_DIR.glob("*.part"))
    # Quarantine leaves the state missing; a later automatic save leaves it valid.
    assert startup(recovery).read_result.status == "missing"
    asyncio.run(persistence.update_user_data(123, {}))
    assert startup(recovery).read_result.status == "valid"
    assert [(task_images.IMAGE_DIR / ref["id"]).read_bytes() for ref in refs] == [
        b"first",
        b"second",
    ]
    # Explicit recovery completion restores normal orphan pruning.
    recovery.with_name("state.json.corrupt").unlink()
    recovery.with_name("state.json.image-recovery").unlink()
    assert startup(recovery).read_result.references_known
    assert task_images.usage()["count"] == 0


@pytest.mark.parametrize("kind", ["missing", "empty", "valid"])
def test_trusted_startup_prunes_orphans(recovery, kind):
    retained = task_images.save(b"live")
    orphan = task_images.save(b"orphan")
    if kind != "missing":
        data = {"tasks": [{"images": [retained]}]} if kind == "valid" else {}
        recovery.write_text(json.dumps({"users": {str(123): data}}))
    result = startup(recovery).read_result
    assert result.status == ("missing" if kind == "missing" else "valid")
    assert result.references_known
    assert not (task_images.IMAGE_DIR / orphan["id"]).exists()
    assert (task_images.IMAGE_DIR / retained["id"]).exists() == (kind == "valid")


def test_unknown_references_remove_parts_and_enforce_cap(recovery, monkeypatch):
    first = task_images.save(b"1111")
    second = task_images.save(b"2222")
    (task_images.IMAGE_DIR / "upload.part").write_bytes(b"unfinished")
    monkeypatch.setattr(task_images, "IMAGE_STORAGE_CAP_BYTES", 4)
    task_images.initialize(None)
    assert not (task_images.IMAGE_DIR / first["id"]).exists()
    assert (task_images.IMAGE_DIR / second["id"]).read_bytes() == b"2222"
    assert not list(task_images.IMAGE_DIR.glob("*.part"))
    assert task_images.usage()["used_bytes"] == 4


@pytest.mark.parametrize(
    "data",
    [
        {"tasks": {}},
        {"task_queue": [None]},
        {"pending_implementation": []},
        {"tasks": [{"images": "bad"}]},
        {"tasks": [{"images": [{}]}]},
        {"pending_plan": {"__dataclass__": "Unknown", "fields": {}}},
        {"pending_plan": {"images": [{"id": []}]}},
    ],
)
def test_unusable_reference_schema_is_untrusted(recovery, data):
    recovery.write_text(json.dumps({"users": {"1": data}}))
    result = read_state_result(recovery)
    assert result.status == "invalid_schema"
    assert not result.references_known


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"tasks": None, "task_queue": [], "pending_implementation": None},
        {"tasks": [{"title": "legacy"}], "pending_plan": {"feature": "legacy"}},
        {
            "pending_plan": {
                "__dataclass__": "PlanState",
                "fields": {
                    "id": "p",
                    "feature": "f",
                    "revision": 1,
                    "plan_text": "t",
                    "approved": False,
                    "removed_field": "legacy",
                },
            }
        },
    ],
)
def test_legacy_reference_schema_remains_trusted(recovery, data):
    recovery.write_text(json.dumps({"users": {"1": data}}))
    result = read_state_result(recovery)
    assert result.status == "valid"
    assert result.references_known


@pytest.mark.parametrize(
    "payload", [[], {}, {"users": []}, {"users": {"bad": {}}}, {"users": {"1": []}}]
)
def test_invalid_envelope_is_empty_and_untrusted(recovery, payload):
    recovery.write_text(json.dumps(payload))
    result = read_state_result(recovery)
    assert result.users == {}
    assert result.status == "invalid_schema"
    assert not result.references_known


def test_valid_empty_users_and_cached_read_result(recovery, monkeypatch):
    recovery.write_text('{"users": {}}')
    persistence = JsonStatePersistence(recovery)
    assert asyncio.run(persistence.get_user_data()) == {}
    assert persistence.read_result.status == "valid"
    assert persistence.read_result.references_known

    def unexpected_read(*args, **kwargs):
        pytest.fail("persistence reread state after initial loading")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    assert asyncio.run(persistence.get_user_data()) == {}
