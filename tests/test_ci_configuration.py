"""Missing workflow runs alone must never be treated as unconfigured CI."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from ai_agent import ci
from ai_agent import ci_configuration as config

REPO = "owner/repo"
HEAD, ROOT, GITHUB, WORKFLOWS = (c * 40 for c in "abcd")
BASE = f"/repos/{REPO}"


def entry(path, sha=GITHUB, kind="tree"):
    return {"path": path, "sha": sha, "type": kind}


@pytest.fixture
def api(monkeypatch):
    responses = {
        BASE + "/actions/workflows": {"total_count": 0, "workflows": []},
        BASE + f"/git/commits/{HEAD}": {"sha": HEAD, "tree": {"sha": ROOT}},
        BASE + f"/git/trees/{ROOT}": {"sha": ROOT, "truncated": False, "tree": []},
        BASE + f"/git/trees/{GITHUB}": {"sha": GITHUB, "truncated": False, "tree": []},
        BASE + f"/git/trees/{WORKFLOWS}": {
            "sha": WORKFLOWS,
            "truncated": False,
            "tree": [],
        },
    }
    calls = []

    def get(method, path, **kwargs):
        assert method == "GET"
        assert kwargs["timeout"] == 10
        calls.append(path)
        return deepcopy(responses[path])

    monkeypatch.setattr(config, "github_request", get)
    return responses, calls


@pytest.mark.parametrize("missing", ["github", "workflows", "yaml"])
def test_positive_absence_at_exact_head(api, missing):
    responses, calls = api
    if missing != "github":
        responses[BASE + f"/git/trees/{ROOT}"]["tree"] = [entry(".github")]
    if missing == "yaml":
        responses[BASE + f"/git/trees/{GITHUB}"]["tree"] = [
            entry("workflows", WORKFLOWS)
        ]
        responses[BASE + f"/git/trees/{WORKFLOWS}"]["tree"] = [
            entry("README.md", HEAD, "blob")
        ]
    assert config.no_actions_workflows(REPO, HEAD)
    assert BASE + f"/git/commits/{HEAD}" in calls


def test_new_workflow_added_in_pr_is_not_mistaken_for_no_ci(api):
    responses, _ = api
    responses[BASE + f"/git/trees/{ROOT}"]["tree"] = [entry(".github")]
    responses[BASE + f"/git/trees/{GITHUB}"]["tree"] = [entry("workflows", WORKFLOWS)]
    responses[BASE + f"/git/trees/{WORKFLOWS}"]["tree"] = [
        entry("tests.yml", HEAD, "blob")
    ]
    assert not config.no_actions_workflows(REPO, HEAD)


def test_registered_or_disabled_workflow_remains_configured(api):
    responses, calls = api
    responses[BASE + "/actions/workflows"] = {
        "total_count": 1,
        "workflows": [{"state": "disabled_manually"}],
    }
    assert not config.no_actions_workflows(REPO, HEAD)
    assert calls == [BASE + "/actions/workflows"]


@pytest.mark.parametrize(
    "change", ["truncated", "wrong_head", "malformed_registry", "malformed_tree"]
)
def test_incomplete_api_evidence_cannot_skip_ci(api, change):
    responses, _ = api
    if change == "truncated":
        responses[BASE + f"/git/trees/{ROOT}"]["truncated"] = True
    if change == "wrong_head":
        responses[BASE + f"/git/commits/{HEAD}"]["sha"] = "e" * 40
    if change == "malformed_registry":
        responses[BASE + "/actions/workflows"] = {}
    if change == "malformed_tree":
        responses[BASE + f"/git/trees/{ROOT}"]["tree"] = ["bad"]
    with pytest.raises(ValueError):
        config.no_actions_workflows(REPO, HEAD)


@pytest.mark.parametrize("error", [RuntimeError("GitHub 403"), TimeoutError("timeout")])
def test_access_failure_propagates_instead_of_bypassing_ci(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(config, "github_request", fail)
    with pytest.raises(type(error)):
        config.no_actions_workflows(REPO, HEAD)


def test_unconfigured_is_a_distinct_state_not_passed():
    with (
        patch.object(ci, "list_workflow_runs", return_value=[]),
        patch.object(ci, "no_actions_workflows", return_value=True) as probe,
    ):
        result = ci.evaluate_ci(HEAD, repository=REPO)
    probe.assert_called_once_with(REPO, HEAD)
    assert result.state == "not_configured"
    assert "CI was not run" in result.summary
    assert "passed" not in result.summary.lower()


def test_queued_workflow_never_probes_or_skips_configuration():
    with (
        patch.object(
            ci,
            "list_workflow_runs",
            return_value=[{"id": 1, "status": "queued", "name": "Tests"}],
        ),
        patch.object(ci, "no_actions_workflows") as probe,
    ):
        assert ci.evaluate_ci(HEAD, repository=REPO).state == "running"
        probe.assert_not_called()


def test_missing_run_response_is_not_an_empty_ci_result():
    with patch.object(ci, "github_request", return_value={}):
        with pytest.raises(ValueError):
            ci.list_workflow_runs(HEAD, REPO)
