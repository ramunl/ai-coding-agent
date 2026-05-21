from dataclasses import dataclass

from ai_agent.config import GITHUB_REPOSITORY
from ai_agent.github import github_request


@dataclass(frozen=True)
class CiResult:
    state: str
    summary: str
    url: str | None = None


def list_workflow_runs(head_sha: str) -> list[dict]:
    response = github_request(
        "GET",
        f"/repos/{GITHUB_REPOSITORY}/actions/runs",
        query={"head_sha": head_sha, "per_page": "20"},
    )
    return response.get("workflow_runs", [])


def list_workflow_jobs(run_id: int) -> list[dict]:
    response = github_request("GET", f"/repos/{GITHUB_REPOSITORY}/actions/runs/{run_id}/jobs")
    return response.get("jobs", [])


def summarize_failed_jobs(runs: list[dict]) -> str:
    lines = []
    for run_data in runs:
        if run_data.get("conclusion") not in {"failure", "cancelled", "timed_out", "action_required"}:
            continue
        jobs = list_workflow_jobs(int(run_data["id"]))
        failed_jobs = [
            job
            for job in jobs
            if job.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"}
        ]
        if not failed_jobs:
            lines.append(f"- {run_data.get('name', 'workflow')} failed: {run_data.get('html_url')}")
            continue
        for job in failed_jobs:
            failed_steps = [
                step.get("name", "unknown step")
                for step in job.get("steps", [])
                if step.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"}
            ]
            step_text = f" ({', '.join(failed_steps[:3])})" if failed_steps else ""
            lines.append(f"- {job.get('name', 'job')}{step_text}: {job.get('html_url')}")
    return "\n".join(lines)


def evaluate_ci(head_sha: str) -> CiResult:
    runs = list_workflow_runs(head_sha)
    if not runs:
        return CiResult("waiting", "CI has not started yet")

    active = [run_data for run_data in runs if run_data.get("status") in {"queued", "requested", "waiting", "pending", "in_progress"}]
    if active:
        names = ", ".join(run_data.get("name", "workflow") for run_data in active[:5])
        return CiResult("running", f"CI running: {names}", active[0].get("html_url"))

    failed = [
        run_data
        for run_data in runs
        if run_data.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"}
    ]
    if failed:
        details = summarize_failed_jobs(failed)
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
        names = ", ".join(run_data.get("name", "workflow") for run_data in successful[:5])
        return CiResult("passed", f"CI passed: {names}", successful[0].get("html_url"))

    return CiResult("waiting", "CI status is not final yet")
