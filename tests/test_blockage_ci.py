"""Successor CI must cover the merged commit and every unfinished workflow."""

from copy import deepcopy

import pytest

from ai_agent import blockage_ci as proof

REPO = "owner/repo"
BASE = f"/repos/{REPO}"
OLD_URL = f"https://github.com/{REPO}/actions/runs/1"
FACTS = {"head": "a" * 40, "merge_commit": "b" * 40, "merged_at": 100}


@pytest.fixture
def evidence(monkeypatch):
    old = {
        "id": 1,
        "workflow_id": 7,
        "repository": {"full_name": REPO},
        "head_sha": "a" * 40,
        "html_url": OLD_URL,
        "status": "queued",
        "conclusion": None,
        "created_at": "1970-01-01T00:00:01Z",
    }
    new = {
        **old,
        "id": 2,
        "head_sha": "c" * 40,
        "html_url": f"https://github.com/{REPO}/actions/runs/2",
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "created_at": "1970-01-01T00:03:20Z",
    }
    data = {
        BASE: {"full_name": REPO, "default_branch": "main"},
        BASE + "/actions/runs": {"workflow_runs": [old], "total_count": 1},
        BASE + "/actions/workflows/7/runs": {"workflow_runs": [new]},
        BASE + "/compare/" + "b" * 40 + "..." + "c" * 40: {
            "status": "ahead",
            "merge_base_commit": {"sha": "b" * 40},
        },
        BASE + "/actions/runs/2/jobs": {
            "total_count": 1,
            "jobs": [{"status": "completed", "conclusion": "success"}],
        },
    }
    calls = []

    def get(method, path, **kwargs):
        assert method == "GET"
        assert kwargs["timeout"] == 10
        calls.append((path, kwargs.get("query")))
        return deepcopy(data[path])

    monkeypatch.setattr(proof, "github_request", get)
    return data, old, new, calls


def test_success_records_both_runs_without_relabelling_original(evidence):
    data, old, new, calls = evidence
    result = proof.superseding_checks(REPO, FACTS, OLD_URL)
    assert result["original_runs"] == [OLD_URL]
    assert result["verified_checks"] == [{"url": new["html_url"], "sha": "c" * 40}]
    assert old["status"] == "queued"
    assert (
        BASE + "/actions/workflows/7/runs",
        {"branch": "main", "event": "push", "per_page": 100},
    ) in calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("conclusion", "failure"),
        ("conclusion", "skipped"),
        ("conclusion", "neutral"),
        ("status", "queued"),
        ("head_branch", "feature/x"),
        ("event", "pull_request"),
        ("created_at", "1970-01-01T00:00:10Z"),
        ("created_at", "invalid"),
        ("workflow_id", 8),
        ("repository", {"full_name": "other/repo"}),
        ("head_sha", "../../bad"),
        ("html_url", "https://evil.test/run"),
    ],
)
def test_wrong_or_unfinished_successor_is_refused(evidence, field, value):
    evidence[2][field] = value
    with pytest.raises(ValueError):
        proof.superseding_checks(REPO, FACTS, OLD_URL)


def test_later_failed_run_cannot_be_hidden_by_older_success(evidence):
    data, old, new, _ = evidence
    data[BASE + "/actions/workflows/7/runs"]["workflow_runs"].append(
        {
            **new,
            "id": 3,
            "html_url": f"https://github.com/{REPO}/actions/runs/3",
            "created_at": "1970-01-01T00:04:00Z",
            "conclusion": "failure",
        }
    )
    with pytest.raises(ValueError):
        proof.superseding_checks(REPO, FACTS, OLD_URL)


@pytest.mark.parametrize("state", ["behind", "diverged", "identical"])
def test_unrelated_ancestry_is_not_evidence(evidence, state):
    compare = evidence[0][BASE + "/compare/" + "b" * 40 + "..." + "c" * 40]
    compare["status"] = state
    with pytest.raises(ValueError):
        proof.superseding_checks(REPO, FACTS, OLD_URL)


def test_failed_original_and_missing_jobs_and_overflow_fail_closed(evidence):
    data, old, new, _ = evidence
    old.update(status="completed", conclusion="failure")
    with pytest.raises(ValueError):
        proof.superseding_checks(REPO, FACTS, OLD_URL)
    old.update(status="queued", conclusion=None)
    data[BASE + "/actions/runs/2/jobs"]["jobs"] = []
    with pytest.raises(ValueError):
        proof.superseding_checks(REPO, FACTS, OLD_URL)
    data[BASE + "/actions/runs"]["total_count"] = 101
    with pytest.raises(ValueError):
        proof.superseding_checks(REPO, FACTS, OLD_URL)


def test_every_unfinished_workflow_requires_its_own_successor(evidence):
    data, old, new, _ = evidence
    data[BASE + "/actions/runs"]["workflow_runs"].append(
        {
            **old,
            "id": 3,
            "workflow_id": 8,
            "html_url": f"https://github.com/{REPO}/actions/runs/3",
        }
    )
    data[BASE + "/actions/runs"]["total_count"] = 2
    data[BASE + "/actions/workflows/8/runs"] = {"workflow_runs": []}
    with pytest.raises(ValueError):
        proof.superseding_checks(REPO, FACTS, OLD_URL)
