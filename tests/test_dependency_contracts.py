"""Local GitHub/Ops provider contract tests; never contact live services."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from ai_agent import deployment_evidence, task_pulls


def test_exact_pr_identity_and_reviewable_contract(monkeypatch):
    head = "a" * 40
    url = "https://github.com/owner/repo/pull/7"
    pull = {
        "number": 7,
        "html_url": url,
        "state": "open",
        "draft": False,
        "merged": False,
        "merged_at": None,
        "base": {"repo": {"full_name": "owner/repo"}},
        "head": {"sha": head, "repo": {"full_name": "owner/repo"}},
    }

    def request(method, path, **kwargs):
        assert (method, path) == ("GET", "/repos/owner/repo/pulls/7")
        assert kwargs == {"timeout": 10, "max_bytes": 262144}
        return deepcopy(pull)

    monkeypatch.setattr(task_pulls, "github_request", request)
    assert task_pulls.verified_pull("owner/repo", url, head)["reviewable"]
    pull["draft"] = True
    assert not task_pulls.verified_pull("owner/repo", url, head)["reviewable"]
    pull["head"]["sha"] = "b" * 40
    with pytest.raises(ValueError, match="identity"):
        task_pulls.verified_pull("owner/repo", url, head)


@pytest.fixture
def deployment():
    # Ops Manager._identity/_history + Store.write contract; verified_at records
    # successful process verification, whereas collection time is irrelevant.
    return {
        "active": None,
        "targets": {
            "ai-coding-agent": {
                "status": "healthy",
                "error": None,
                "operation": "deploy1",
                "current": {
                    "commit": "a" * 40,
                    "version": "1",
                    "ref": "main",
                    "verified_at": datetime.fromtimestamp(1000, UTC).isoformat(),
                },
                "history": [
                    {
                        "action": "deploy",
                        "status": "healthy",
                        "operation": "deploy1",
                        "at": datetime.fromtimestamp(1001, UTC).isoformat(),
                    }
                ],
            }
        },
    }


def test_authoritative_deployment_contract(deployment):
    assert (
        deployment_evidence.validate_deployment(deployment, "ai-coding-agent", 1010)[
            "commit"
        ]
        == "a" * 40
    )


@pytest.mark.parametrize(
    "change",
    [
        "rollback",
        "old",
        "future",
        "active",
        "unhealthy",
        "naive",
        "missing",
        "missing_active",
        "missing_commit",
    ],
)
def test_deployment_rejects_unverified_or_stale(deployment, change):
    record = deployment["targets"]["ai-coding-agent"]
    if change == "missing_active":
        del deployment["active"]
    elif change == "missing_commit":
        del record["current"]["commit"]
    elif change == "rollback":
        record["history"][-1]["action"] = "rollback"
    elif change == "active":
        deployment["active"] = {"action": "deploy"}
    elif change == "unhealthy":
        record["status"] = "failed"
    elif change == "naive":
        record["current"]["verified_at"] = "2026-01-01T00:00:00"
    elif change == "missing":
        del record["current"]
    now = 2000 if change == "old" else 999 if change == "future" else 1010
    with pytest.raises((ValueError, KeyError)):
        deployment_evidence.validate_deployment(deployment, "ai-coding-agent", now)


def test_reader_is_bounded_read_only_and_rejects_links(
    tmp_path, monkeypatch, deployment
):
    import json

    path = tmp_path / "state.json"
    path.write_text(json.dumps(deployment))
    monkeypatch.setattr(deployment_evidence, "STATE_PATH", str(path))
    assert deployment_evidence.read_state() == deployment
    path.write_bytes(b"x" * 262145)
    with pytest.raises(ValueError):
        deployment_evidence.read_state()
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError):
        deployment_evidence.read_state()
