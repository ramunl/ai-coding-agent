"""Identity isolation and allowlist boundaries for context publication."""

import copy
import json
from types import SimpleNamespace

import pytest

from ai_agent.context_projection import initiative_statuses, task_context_status


def receipt(task_id="task", initiative_id="initiative"):
    return {
        "id": task_id,
        "context_spec": {
            "task_id": task_id,
            "initiative_id": initiative_id,
            "attempt_id": "attempt",
            "scope": {"owner_id": "PRIVATE", "repo_path": "/PRIVATE"},
        },
        "packet": {"packet_id": "packet", "sha256": "a" * 64, "records": "PRIVATE"},
        "packet_metadata": {
            "schema_version": 1,
            "verified": True,
            "observed_at": 100,
            "omission_count": 0,
            "truncation_count": 0,
            "summary": "PRIVATE",
        },
        "transcript": "PRIVATE",
        "error": "PRIVATE",
    }


@pytest.mark.parametrize("bad", [None, True, 1, [], {}, "../private", "x" * 65, "é"])
@pytest.mark.parametrize("field", ["task_id", "initiative_id", "attempt_id"])
def test_malformed_identity_is_unknown(bad, field):
    task = receipt()
    task["context_spec"][field] = bad
    assert task_context_status(task, {}, now=100)["health"] == "unknown"
    assert initiative_statuses({"tasks": [task]}, now=100) == []


@pytest.mark.parametrize(
    "value, expected",
    [(True, None), (-1, None), (1.5, None), ("0", None), (65536, 65535)],
)
def test_count_bounds(value, expected):
    task = receipt()
    task["packet_metadata"]["omission_count"] = value
    status = task_context_status(task, {}, now=100)
    assert status["omission_count"] == expected
    assert status["health"] == ("unknown" if expected is None else "degraded")


@pytest.mark.parametrize(
    "now, health", [(99, "unknown"), (160, "healthy"), (161, "healthy")]
)
def test_health_does_not_treat_capture_age_as_a_defect(now, health):
    assert task_context_status(receipt(), {}, now=now)["health"] == health


def test_missing_or_unverified_receipt_is_unknown():
    for metadata in [None, {}, {"schema_version": 2}, {"verified": True}]:
        task = receipt()
        task["packet_metadata"] = metadata
        assert task_context_status(task, {}, now=100)["health"] == "unknown"


def test_matching_plan_takes_precedence_without_borrowing_task_metadata():
    task = receipt()
    task["plan_id"] = "plan"
    plan = SimpleNamespace(
        id="plan", context_spec=task["context_spec"], context_packet=task["packet"]
    )
    status = task_context_status(task, {"pending_plan": plan}, now=100)
    assert status["freshness"] == "unknown"
    plan.context_spec = receipt("other")["context_spec"]
    assert (
        task_context_status(task, {"pending_plan": plan}, now=100)["packet_refs"] == []
    )


def test_initiatives_are_sorted_unique_and_aggregate_conservatively():
    tasks = [receipt("a", "z"), receipt("b", "a"), receipt("c", "z")]
    rows = initiative_statuses({"tasks": tasks}, now=100)
    assert [row["id"] for row in rows] == ["a", "z"]
    assert all(row["context_status"]["health"] == "healthy" for row in rows)
    assert all(
        row["context_status"]["evidence_state"] == "verified_current" for row in rows
    )
    assert rows == initiative_statuses({"tasks": tasks[::-1]}, now=100)
    # One stale receipt keeps the whole initiative coverage unknown.
    stale = receipt("a", "z")
    stale["packet_metadata"]["observed_at"] = 39
    rows = initiative_statuses({"tasks": [stale, receipt("b", "z")]}, now=100)
    assert [row["id"] for row in rows] == ["z"]
    assert rows[0]["context_status"]["health"] == "unknown"
    assert rows[0]["context_status"]["evidence_state"] == "stale"


def test_evidence_state_distinguishes_not_planned_legacy_and_unavailable():
    # A task that can still be planned is known to carry no identity yet.
    todo = {"id": "t", "stage": "todo"}
    assert task_context_status(todo, {}, now=100)["evidence_state"] == "not_planned"
    # A finished legacy task without an identity stays unknown.
    done = {"id": "t", "stage": "done"}
    assert task_context_status(done, {}, now=100)["evidence_state"] == "unknown"
    # An identity with unverifiable metadata is metadata-unavailable, not stale.
    task = receipt()
    task["packet_metadata"] = {"schema_version": 1}
    status = task_context_status(task, {}, now=100)
    assert status["evidence_state"] == "metadata_unavailable"
    assert status["health"] == "unknown"


def test_initiative_coverage_reports_worst_evidence_state():
    tasks = [receipt("a", "z"), receipt("b", "z")]
    tasks[1]["packet_metadata"] = {"schema_version": 1}
    rows = initiative_statuses({"tasks": tasks}, now=100)
    assert [row["id"] for row in rows] == ["z"]
    assert rows[0]["context_status"]["health"] == "unknown"
    assert rows[0]["context_status"]["evidence_state"] == "metadata_unavailable"


def test_private_data_and_arbitrary_enums_are_excluded_without_mutation():
    task = receipt()
    task.update(health="PRIVATE", freshness="PRIVATE", dependency_blockers=["PRIVATE"])
    before = copy.deepcopy(task)
    status = task_context_status(task, {}, now=100)
    assert status["health"] == "healthy"
    assert "PRIVATE" not in json.dumps(status)
    assert task == before


@pytest.mark.parametrize(
    "fact, health, code",
    [
        ("invalidated", "blocked", "dependency_changed"),
        ("access_failed", "unavailable", "dependency_unavailable"),
        ("recovery_pending", "blocked", "dependency_recovery_pending"),
    ],
)
def test_dependency_facts_require_matching_fresh_verified_observation(
    fact, health, code
):
    task = receipt()
    task["after"] = "previous"
    task["dependency_check"] = {"reason": "dependency unavailable PRIVATE"}
    assert task_context_status(task, {}, now=100)["dependency_blockers"] == [
        "dependency_unknown"
    ]
    observation = {
        "context_spec": copy.deepcopy(task["context_spec"]),
        "metadata": task["packet_metadata"],
        fact: True,
        "reason": "PRIVATE",
    }
    task["dependency_observation"] = observation
    status = task_context_status(task, {}, now=100)
    assert status["health"] == health
    assert status["dependency_blockers"] == [code]
    assert "PRIVATE" not in json.dumps(status)
    assert task_context_status(task, {}, now=161)["dependency_blockers"] == [
        "dependency_unknown"
    ]
    observation["context_spec"]["attempt_id"] = "other"
    assert task_context_status(task, {}, now=100)["health"] == "unknown"


def test_invalid_packet_and_timestamp_are_not_published():
    task = receipt()
    task["packet"]["packet_id"] = "/PRIVATE"
    task["packet_metadata"]["observed_at"] = True
    status = task_context_status(task, {}, now=100)
    assert status["packet_refs"] == []
    assert status["observed_at"] is None
    assert status["health"] == "unknown"


def test_retained_run_is_isolated_from_other_tasks_and_receipts():
    task = receipt()
    task["run"] = receipt("other")
    data = {"tasks": [task, receipt("other")]}
    assert task_context_status(task, data, now=100)["packet_refs"] == []
    task["run"] = receipt()
    task["packet_metadata"]["observed_at"] = 999
    assert task_context_status(task, data, now=100)["health"] == "healthy"


def test_verified_dependency_success_clears_only_structured_blockers():
    task = receipt()
    task["dependency_ids"] = ["previous"]
    task["dependency_observation"] = {
        "context_spec": task["context_spec"],
        "metadata": task["packet_metadata"],
        "status": "satisfied",
        "code": "dependency_unavailable",
    }
    assert task_context_status(task, {}, now=100)["health"] == "healthy"
    task["dependency_observation"]["status"] = "PRIVATE"
    assert task_context_status(task, {}, now=100)["health"] == "unknown"
    task["packet_metadata"]["verified"] = False
    task["dependency_observation"]["access_failed"] = True
    assert task_context_status(task, {}, now=100)["dependency_blockers"] == [
        "dependency_unknown"
    ]
