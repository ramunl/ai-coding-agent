"""Summarize GitHub workflow results for implementation and repair runs."""

from dataclasses import dataclass
from pathlib import Path

from ai_agent.ci_configuration import no_actions_workflows
from ai_agent.github import github_request
from ai_agent.projects import active_project
from ai_agent.verification_summary import summarize_verification


@dataclass(frozen=True)
class CiResult:
    """Describe the aggregate CI state, summary, and optional workflow URL."""

    state: str
    summary: str
    url: str | None = None


def list_workflow_runs(head_sha: str, repository: str | None = None) -> list[dict]:
    """Fetch workflow runs associated with the requested commit."""
    response = github_request(
        "GET",
        f"/repos/{repository or active_project().github_repository}/actions/runs",
        query={"head_sha": head_sha, "per_page": "20"},
    )
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("workflow_runs"), list)
        or (not response["workflow_runs"] and response.get("total_count") != 0)
    ):
        raise ValueError("GitHub workflow run evidence is unavailable")
    return response["workflow_runs"]


def list_workflow_jobs(run_id: int, repository: str | None = None) -> list[dict]:
    """Fetch the jobs belonging to a workflow run."""
    selected = repository or active_project().github_repository
    response = github_request("GET", f"/repos/{selected}/actions/runs/{run_id}/jobs")
    return response.get("jobs", [])


def summarize_failed_jobs(runs: list[dict], repository: str | None = None) -> str:
    """Describe failed workflow jobs and their failing steps."""
    lines = []
    for run_data in runs:
        if run_data.get("conclusion") not in {
            "failure",
            "cancelled",
            "timed_out",
            "action_required",
        }:
            continue
        jobs = (
            list_workflow_jobs(int(run_data["id"]), repository)
            if repository
            else list_workflow_jobs(int(run_data["id"]))
        )
        failed_jobs = [
            job
            for job in jobs
            if job.get("conclusion")
            in {"failure", "cancelled", "timed_out", "action_required"}
        ]
        if not failed_jobs:
            lines.append(
                (
                    f"- {run_data.get('name', 'workflow')} failed: "
                    f"{run_data.get('html_url')}"
                )
            )
            continue
        for job in failed_jobs:
            failed_steps = [
                step.get("name", "unknown step")
                for step in job.get("steps", [])
                if step.get("conclusion")
                in {"failure", "cancelled", "timed_out", "action_required"}
            ]
            step_text = f" ({', '.join(failed_steps[:3])})" if failed_steps else ""
            lines.append(
                f"- {job.get('name', 'job')}{step_text}: {job.get('html_url')}"
            )
    return "\n".join(lines)


def latest_runs_by_workflow(runs: list[dict]) -> list[dict]:
    """Keep the latest attempt of each workflow."""
    latest: dict[str, dict] = {}
    for run_data in runs:
        workflow_key = str(
            run_data.get("workflow_id") or run_data.get("name") or run_data.get("id")
        )
        current = latest.get(workflow_key)
        if current is None or run_sort_key(run_data) > run_sort_key(current):
            latest[workflow_key] = run_data
    return list(latest.values())


def run_sort_key(run_data: dict) -> tuple[str, int, int]:
    """Order runs by creation time, attempt number, and identifier."""
    return (
        str(run_data.get("created_at") or run_data.get("run_started_at") or ""),
        int(run_data.get("run_attempt") or 0),
        int(run_data.get("id") or 0),
    )


def evaluate_ci(head_sha: str, *, repository: str | None = None) -> CiResult:
    """Classify the latest workflow runs for a commit."""
    runs = latest_runs_by_workflow(
        list_workflow_runs(head_sha, repository)
        if repository
        else list_workflow_runs(head_sha)
    )
    if not runs:
        selected = repository or active_project().github_repository
        if no_actions_workflows(selected, head_sha):
            return CiResult(
                "not_configured",
                "CI not configured: no GitHub Actions workflows. PR is open for "
                "review; CI was not run. Local verification is reported separately.",
            )
        return CiResult("waiting", "CI has not started yet")

    active = [
        run_data
        for run_data in runs
        if run_data.get("status")
        in {"queued", "requested", "waiting", "pending", "in_progress"}
    ]
    if active:
        names = ", ".join(run_data.get("name", "workflow") for run_data in active[:5])
        return CiResult("running", f"CI running: {names}", active[0].get("html_url"))

    failed = [
        run_data
        for run_data in runs
        if run_data.get("conclusion")
        in {"failure", "cancelled", "timed_out", "action_required"}
    ]
    if failed:
        details = (
            summarize_failed_jobs(failed, repository)
            if repository
            else summarize_failed_jobs(failed)
        )
        summary = "CI failed"
        if details:
            summary = f"{summary}:\n{details}"
        return CiResult("failed", summary, failed[0].get("html_url"))

    successful = [
        run_data
        for run_data in runs
        if run_data.get("conclusion") in {"success", "skipped", "neutral"}
    ]
    if successful:
        names = ", ".join(
            run_data.get("name", "workflow") for run_data in successful[:5]
        )
        return CiResult("passed", f"CI passed: {names}", successful[0].get("html_url"))

    return CiResult("waiting", "CI status is not final yet")


def build_failure_context(
    pr_number: int,
    result: CiResult,
    max_chars: int = 3500,
    artifact_dir: Path | None = None,
) -> str:
    """Combine CI results and build comments into a bounded, redacted summary.

    Raw job/comment logs never reach the model: the combined failure text is
    passed through ``verification_summary`` so only redacted head/tail lines,
    counts and an explicit truncation marker enter the repair prompt. Full
    redacted diagnostics are retained in ``artifact_dir`` when provided.
    """
    comments = github_request(
        "GET",
        f"/repos/{active_project().github_repository}/issues/{pr_number}/comments",
        query={"per_page": "30"},
    )
    failure_comments = [
        str(comment.get("body", "")).strip()
        for comment in comments
        if "Build failed" in str(comment.get("body", ""))
    ]
    parts = [result.summary]
    if result.url:
        parts.append(result.url)
    if failure_comments:
        parts.extend(["Recent build failure comment:", failure_comments[-1]])
    raw = "\n\n".join(part for part in parts if part)
    summary = summarize_verification(
        raw,
        identity=f"ci-failure-pr-{pr_number}",
        exit_code=1,
        max_chars=max_chars,
        artifact_dir=artifact_dir,
    )
    return summary.text()
