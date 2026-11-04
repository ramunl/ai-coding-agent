"""Deterministic snapshot-level context status tests through build_content."""

import importlib.util
import json
import sys
import types
from types import SimpleNamespace

import pytest

from ai_agent.context_models import PacketReference
from ai_agent.context_status import (
    MAX_COUNT,
    MAX_PACKET_REFS,
    PacketMetadata,
    context_status,
)

SHA256 = "a" * 64

# The bot snapshot publisher transitively imports the Anthropic client. These
# tests never call it, so install a minimal stand-in when the SDK is absent to
# keep the snapshot imports deterministic offline. A spec-less stand-in already
# registered by another test is treated as sufficient.
try:
    _anthropic_available = importlib.util.find_spec("anthropic") is not None
except ValueError:
    _anthropic_available = True
if not _anthropic_available:
    _anthropic = types.ModuleType("anthropic")
    _anthropic.Anthropic = lambda api_key, **kwargs: object()
    _anthropic.DefaultHttpxClient = lambda **kwargs: object()
    _anthropic.NotFoundError = type("NotFoundError", (Exception,), {})
    sys.modules["anthropic"] = _anthropic


def _state_module():
    from ai_agent.bot import state

    return state


def _build_content():
    from ai_agent.bot.snapshot_publisher import build_content

    return build_content


def receipt(
    *,
    task_id="task",
    initiative_id="initiative",
    attempt_id="attempt",
    packet_id="packet",
    sha256=SHA256,
    observed_at=100,
    verified=True,
    omission_count=0,
    truncation_count=0,
):
    """Build a deterministic, identity-bound receipt with an allowlisted packet."""
    return {
        "id": task_id,
        "context_spec": {
            "task_id": task_id,
            "initiative_id": initiative_id,
            "attempt_id": attempt_id,
        },
        "packet": {"packet_id": packet_id, "sha256": sha256},
        "packet_metadata": {
            "schema_version": 1,
            "verified": verified,
            "observed_at": observed_at,
            "omission_count": omission_count,
            "truncation_count": truncation_count,
        },
    }


def legacy_task(task_id="legacy-1"):
    """A task without any context_spec/packet/metadata observations."""
    return {
        "id": task_id,
        "title": "Legacy task",
        "stage": "done",
        "note": "old note",
        "branch": "feature/old",
    }


def dependency_receipt(task_id="task", *, fact=None):
    """A receipt carrying a free-text dependency edge, optionally a structured fact."""
    task = receipt(task_id=task_id)
    task["after"] = "previous"
    if fact is not None:
        task["dependency_observation"] = {
            "context_spec": task["context_spec"],
            "metadata": task["packet_metadata"],
            fact: True,
        }
    return task


def snapshot_content(user_data, *, now=100.0, monkeypatch):
    """Run build_content with a fixed clock for snapshot() and worker_view()."""
    monkeypatch.setattr(_state_module(), "time", SimpleNamespace(time=lambda: now))
    return _build_content()(
        user_data,
        {"name": "demo", "repository": "owner/demo", "branch": "main"},
        {"version": "0.0.0", "core": "core"},
    )


def status_of(task, *, now=100.0, monkeypatch):
    """Return the published context status for the first task in the snapshot."""
    content = snapshot_content({"tasks": [task]}, now=now, monkeypatch=monkeypatch)
    return content["tasks"][0]["context_status"]


def test_health_precedence_through_build_content(monkeypatch):
    status = status_of(receipt(), monkeypatch=monkeypatch)
    assert status["health"] == "healthy"
    status = status_of(receipt(), now=161.0, monkeypatch=monkeypatch)
    assert status["health"] == "degraded"
    blocked = dependency_receipt(fact="invalidated")
    assert status_of(blocked, monkeypatch=monkeypatch)["health"] == "blocked"
    status = status_of(receipt(verified=False), monkeypatch=monkeypatch)
    assert status["health"] == "unknown"
    unavailable = dependency_receipt(fact="access_failed")
    assert status_of(unavailable, monkeypatch=monkeypatch)["health"] == "unavailable"
    unavailable["dependency_observation"]["invalidated"] = True
    assert status_of(unavailable, monkeypatch=monkeypatch)["health"] == "unavailable"


def test_packet_refs_allowlist_and_invalid_identity(monkeypatch):
    status = status_of(
        receipt(packet_id="pkt-1", sha256="b" * 64),
        monkeypatch=monkeypatch,
    )
    assert status["packet_refs"] == [{"packet_id": "pkt-1", "sha256": "b" * 64}]
    status = status_of(receipt(packet_id="../private"), monkeypatch=monkeypatch)
    assert status["packet_refs"] == []
    assert status["health"] == "unknown"


def test_packet_ref_cap_and_duplicate_collapse():
    refs = tuple(PacketReference(f"p{i:02}", SHA256) for i in range(12))
    status = context_status(refs, PacketMetadata(100, True, 0, 0), now=100)
    assert status.health == "degraded"
    assert len(status.to_dict()["packet_refs"]) == MAX_PACKET_REFS
    collapsed = context_status(
        (refs[0], refs[0], refs[1]), PacketMetadata(100, True, 0, 0), now=100
    )
    assert collapsed.health == "healthy"
    assert len(collapsed.to_dict()["packet_refs"]) == 2


@pytest.mark.parametrize(
    ("now", "freshness", "health"),
    [
        (100, "fresh", "healthy"),
        (160, "fresh", "healthy"),
        (161, "stale", "degraded"),
        (99, "unknown", "unknown"),
    ],
)
def test_freshness_matrix(monkeypatch, now, freshness, health):
    status = status_of(receipt(), now=float(now), monkeypatch=monkeypatch)
    assert status["freshness"] == freshness
    assert status["health"] == health


def test_truncation_count_bounds_and_saturation(monkeypatch):
    status = status_of(receipt(truncation_count=1), monkeypatch=monkeypatch)
    assert status["truncation_count"] == 1
    assert status["health"] == "degraded"
    status = status_of(
        receipt(truncation_count=MAX_COUNT + 1),
        monkeypatch=monkeypatch,
    )
    assert status["truncation_count"] == MAX_COUNT
    assert status["health"] == "degraded"
    for invalid in (-1, True, 1.5, "0"):
        status = status_of(receipt(truncation_count=invalid), monkeypatch=monkeypatch)
        assert status["truncation_count"] is None
        assert status["health"] == "unknown"


@pytest.mark.parametrize(
    ("fact", "code", "health"),
    [
        ("access_failed", "dependency_unavailable", "unavailable"),
        ("invalidated", "dependency_changed", "blocked"),
        ("recovery_pending", "dependency_recovery_pending", "blocked"),
    ],
)
def test_structured_dependency_blocker_codes(monkeypatch, fact, code, health):
    status = status_of(dependency_receipt(fact=fact), monkeypatch=monkeypatch)
    assert status["dependency_blockers"] == [code]
    assert status["health"] == health


def test_dependency_unknown_fallback(monkeypatch):
    status = status_of(dependency_receipt(), monkeypatch=monkeypatch)
    assert status["dependency_blockers"] == ["dependency_unknown"]
    assert status["health"] == "unknown"

    task = dependency_receipt()
    task["dependency_observation"] = {
        "context_spec": task["context_spec"],
        "metadata": task["packet_metadata"],
        "status": "blocked",
    }
    status = status_of(task, monkeypatch=monkeypatch)
    assert status["dependency_blockers"] == ["dependency_unknown"]

    task = dependency_receipt(fact="invalidated")
    task["dependency_observation"]["context_spec"] = {
        "task_id": "other",
        "initiative_id": "other",
        "attempt_id": "other",
    }
    status = status_of(task, monkeypatch=monkeypatch)
    assert status["dependency_blockers"] == ["dependency_unknown"]
    assert status["health"] == "unknown"

    task = dependency_receipt(fact="invalidated")
    task["dependency_observation"]["metadata"] = {
        **task["packet_metadata"],
        "observed_at": 39,
    }
    status = status_of(task, now=100.0, monkeypatch=monkeypatch)
    assert status["dependency_blockers"] == ["dependency_unknown"]
    assert status["health"] == "unknown"


def test_seeded_secrets_and_private_paths_stay_out(monkeypatch):
    secret = "token=seeded-secret"
    private = "/private/repository"
    task = receipt()
    task["context_spec"]["scope"] = {"owner_id": secret, "repo_path": private}
    task["context_spec"]["summary"] = secret
    task["packet"]["records"] = [{"prompt": secret, "path": private}]
    task["packet_metadata"]["summary"] = secret
    task["transcript"] = secret
    task["error"] = private
    task["after"] = "previous"
    task["dependency_observation"] = {
        "context_spec": task["context_spec"],
        "metadata": task["packet_metadata"],
        "reason": secret,
        "exception": private,
        "status": "blocked",
    }
    content = snapshot_content({"tasks": [task]}, monkeypatch=monkeypatch)
    serialized = json.dumps(content, sort_keys=True)
    for value in (secret, private):
        assert value not in serialized
    status = content["tasks"][0]["context_status"]
    assert status["packet_refs"] == [{"packet_id": "packet", "sha256": SHA256}]
    assert status["dependency_blockers"] == ["dependency_unknown"]
    assert [row["id"] for row in content["initiatives"]] == ["initiative"]


def test_legacy_tasks_load_unknown_and_serialize(monkeypatch):
    content = snapshot_content({"tasks": [legacy_task()]}, monkeypatch=monkeypatch)
    status = content["tasks"][0]["context_status"]
    assert status["health"] == "unknown"
    assert status["freshness"] == "unknown"
    assert status["packet_refs"] == []
    assert status["dependency_blockers"] == []
    assert content["initiatives"] == []
    json.dumps(content, sort_keys=True)
