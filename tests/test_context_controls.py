"""Confirmation claims survive races, restart and uncertain archive outcomes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from unittest.mock import patch

import pytest

from ai_agent.context_controls import (
    CONTEXT_ACTIONS,
    Confirmations,
    command_args,
)
from ai_agent.context_management import ManagementError, OwnerEvent
from ai_agent.context_models import Scope

ARGS = dict(
    scope="owner", kind="owner", record_id="notes", revision="1", decision_id="choice"
)
ACTION = "context_approve_v1"
EVENT = OwnerEvent("123", "123", 99)


def offer(store, request_id="abcdef12", now=1000):
    return store.offer(request_id, ACTION, ARGS, asdict(Scope("123")), now, now=now)


def test_concurrent_claim_and_restart(tmp_path):
    path = tmp_path / "claims.db"
    token = offer(Confirmations(path))

    def consume(_):
        try:
            return Confirmations(path).consume(token, ACTION, EVENT, "123", now=1001)
        except ManagementError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(result is not None for result in pool.map(consume, range(8))) == 1
    assert consume(None) is None
    with pytest.raises(ManagementError, match="already claimed"):
        offer(Confirmations(path))


def test_concurrent_offers_only_one_token(tmp_path):
    def create(_):
        try:
            return offer(Confirmations(tmp_path / "claims.db"))
        except ManagementError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(value is not None for value in pool.map(create, range(8))) == 1


def test_expiry_cleanup_never_revives_old_token(tmp_path):
    store = Confirmations(tmp_path / "claims.db")
    old = offer(store)
    with pytest.raises(ManagementError):
        store.consume(old, ACTION, EVENT, "123", now=1601)
    new = offer(store, now=1601)
    assert new != old
    with pytest.raises(ManagementError):
        store.consume(old, ACTION, EVENT, "123", now=1601)
    store.consume(new, ACTION, EVENT, "123", now=1601)
    with pytest.raises(ManagementError):
        store.offer("12345678", ACTION, ARGS, asdict(Scope("123")), 1000, now=1601)


def test_mismatch_and_unauthorized_events_do_not_consume(tmp_path):
    store = Confirmations(tmp_path / "claims.db")
    token = offer(store)
    for event in (
        OwnerEvent("456", "123", 1),
        OwnerEvent("123", "456", 1),
        OwnerEvent("123", "123", True),
        {"actor_id": "123"},
    ):
        with patch.object(store, "_transaction") as access:
            with pytest.raises(ManagementError):
                store.consume(token, ACTION, event, "123", now=1001)
        access.assert_not_called()
    with pytest.raises(ManagementError, match="match"):
        store.consume(token, "context_reject_v1", EVENT, "123", now=1001)
    assert store.consume(token, ACTION, EVENT, "123", now=1001)["args"] == ARGS


def test_schemas_are_exact_and_bounded():
    for action in CONTEXT_ACTIONS:
        args = {key: ARGS[key] for key in ("scope", "kind", "record_id", "revision")}
        if "link" in action:
            args.update(
                target_kind="attempt", target_id="attempt", target_revision="1000000"
            )
        elif "inspect" not in action:
            args["decision_id"] = "choice"
        assert command_args(action, args)
        for key in ("actor", "approval", "provenance", "actor_id", "event_id"):
            with pytest.raises(ManagementError):
                command_args(action, {**args, key: "123"})
        for key, value in (
            ("revision", True),
            ("revision", "1000001"),
            ("revision", "0"),
            ("record_id", "a" * 65),
            ("record_id", "../notes"),
            ("scope", "active"),
        ):
            with pytest.raises(ManagementError):
                command_args(action, {**args, key: value})


def test_capacity_and_storage_failure_fail_closed(tmp_path):
    store = Confirmations(tmp_path / "claims.db")
    with patch("ai_agent.context_controls.MAX_BINDINGS", 1):
        token = offer(store)
        with pytest.raises(ManagementError, match="capacity"):
            offer(store, "12345678")
        store.consume(token, ACTION, EVENT, "123", now=1001)
        with pytest.raises(ManagementError, match="capacity"):
            offer(store, "12345678")
    file = tmp_path / "not_a_directory"
    file.write_text("blocked")
    with pytest.raises(OSError):
        offer(Confirmations(file / "claims.db"))
