"""Read-only proof that default-branch CI supersedes an unfinished PR check."""

import re
from datetime import datetime

from ai_agent.ci import latest_runs_by_workflow, run_sort_key
from ai_agent.github import github_request

SHA = re.compile(r"[0-9a-f]{40}")
ACTIVE = {"queued", "requested", "waiting", "pending", "in_progress"}


def _get(path: str, **query: object) -> dict:
    result = github_request(
        "GET", path, query=query or None, timeout=10, max_bytes=1024 * 1024
    )
    if not isinstance(result, dict):
        raise ValueError("GitHub returned incomplete CI evidence")
    return result


def _runs(result: dict) -> list[dict]:
    values = result.get("workflow_runs")
    if not isinstance(values, list) or not values or len(values) > 100:
        raise ValueError("No bounded workflow evidence is available")
    if any(not isinstance(run, dict) for run in values):
        raise ValueError("Malformed workflow evidence")
    return values


def _run(run: dict, repository: str) -> None:
    if (
        type(run.get("id")) is not int
        or run["id"] <= 0
        or type(run.get("workflow_id")) is not int
        or run["workflow_id"] <= 0
        or (run.get("repository") or {}).get("full_name") != repository
        or not isinstance(run.get("head_sha"), str)
        or not SHA.fullmatch(run["head_sha"])
        or run.get("html_url")
        != f"https://github.com/{repository}/actions/runs/{run['id']}"
    ):
        raise ValueError("Workflow identity does not match the task repository")


def superseding_checks(repository: str, facts: dict, original_url: str) -> dict:
    """Require every latest PR workflow to pass or have newer covering push CI.

    Only actual success counts. Skipped/neutral, older, unrelated, failed and
    still-running default-branch workflows are not proof. The original checks
    are observed, never cancelled or relabelled as passed.
    """
    base = f"/repos/{repository}"
    repo = _get(base)
    branch = repo.get("default_branch")
    if repo.get("full_name") != repository or not isinstance(branch, str) or not branch:
        raise ValueError("The repository default branch is unavailable")
    response = _get(f"{base}/actions/runs", head_sha=facts["head"], per_page=100)
    originals = _runs(response)
    if (
        type(response.get("total_count")) is not int
        or response["total_count"] > 100
        or response["total_count"] != len(originals)
    ):
        raise ValueError("PR workflow evidence is incomplete or exceeds the limit")
    for run in originals:
        _run(run, repository)
        if run["head_sha"] != facts["head"]:
            raise ValueError("PR workflow commit does not match")
    latest = latest_runs_by_workflow(originals)
    if len(latest) > 10:
        raise ValueError("Too many CI workflows to resolve in one bounded check")
    if original_url not in {run["html_url"] for run in latest}:
        raise ValueError("The saved CI run is no longer the latest PR check")
    unresolved, replacements = [], []
    for old in latest:
        if old.get("status") == "completed" and old.get("conclusion") == "success":
            continue
        if old.get("status") not in ACTIVE:
            raise ValueError(
                "A PR check failed or is unknown; repair it before resolving"
            )
        runs = _runs(
            _get(
                f"{base}/actions/workflows/{old['workflow_id']}/runs",
                branch=branch,
                event="push",
                per_page=100,
            )
        )
        for run in runs:
            _run(run, repository)
            if (
                run["workflow_id"] != old["workflow_id"]
                or run.get("event") != "push"
                or run.get("head_branch") != branch
            ):
                raise ValueError("Default-branch workflow identity does not match")
        new = max(runs, key=run_sort_key)
        try:
            created = datetime.fromisoformat(new["created_at"].replace("Z", "+00:00"))
            later = (
                created.tzinfo is not None and created.timestamp() >= facts["merged_at"]
            )
        except (KeyError, TypeError, ValueError):
            later = False
        if (
            not later
            or new.get("status") != "completed"
            or new.get("conclusion") != "success"
        ):
            raise ValueError(
                "The latest default-branch check has not succeeded after merge"
            )
        if new["head_sha"] != facts["merge_commit"]:
            compare = _get(
                f"{base}/compare/{facts['merge_commit']}...{new['head_sha']}"
            )
            if (
                compare.get("status") != "ahead"
                or (compare.get("merge_base_commit") or {}).get("sha")
                != facts["merge_commit"]
            ):
                raise ValueError(
                    "Successful CI does not include the task's merge commit"
                )
        jobs = _get(f"{base}/actions/runs/{new['id']}/jobs", per_page=100)
        values = jobs.get("jobs")
        if (
            not isinstance(values, list)
            or not values
            or jobs.get("total_count") != len(values)
            or any(not isinstance(job, dict) for job in values)
            or not any(job.get("conclusion") == "success" for job in values)
            or any(
                job.get("status") != "completed"
                or job.get("conclusion") not in {"success", "skipped"}
                for job in values
            )
        ):
            raise ValueError("Successful workflow jobs cannot be verified")
        unresolved.append(old["html_url"])
        replacements.append({"url": new["html_url"], "sha": new["head_sha"]})
    if not unresolved:
        raise ValueError(
            "No unresolved CI blockage remains; use normal completion tracking"
        )
    return {
        "original_runs": unresolved,
        "verified_checks": replacements,
        "branch": branch,
    }
