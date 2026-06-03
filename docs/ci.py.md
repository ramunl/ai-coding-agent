# ci.py

**Purpose**: Monitor and evaluate GitHub Actions CI/CD pipeline status.

## Overview
This module queries GitHub Actions workflow runs and jobs to determine the current CI status and provide meaningful summaries to the user.

## Data Structures

### `CiResult` (dataclass)
Represents the overall CI status.

**Fields**:
- `state: str` - One of: "waiting", "running", "passed", "failed"
- `summary: str` - Human-readable status message
- `url: str | None` - Link to the GitHub Actions workflow run

## Key Functions

### `list_workflow_runs(head_sha: str) -> list[dict]`
Fetches GitHub Actions workflow runs for a specific commit.

**Parameters**:
- `head_sha`: Git commit SHA to check

**Returns**: List of workflow run objects with:
- `id`: Run ID
- `name`: Workflow name
- `status`: Current status (queued, in_progress, completed, etc)
- `conclusion`: Final result (success, failure, cancelled, etc)
- `html_url`: Link to the run

### `list_workflow_jobs(run_id: int) -> list[dict]`
Fetches individual jobs for a workflow run.

**Parameters**:
- `run_id`: Workflow run ID

**Returns**: List of job objects with:
- `name`: Job name
- `status`: Job status
- `conclusion`: Job conclusion
- `steps[]`: Array of step objects with name and conclusion
- `html_url`: Link to the job

### `summarize_failed_jobs(runs: list[dict]) -> str`
Creates a detailed summary of failed jobs across multiple runs.

**Parameters**:
- `runs`: List of workflow runs to analyze

**Returns**: Formatted markdown string listing:
- Failed jobs by name
- Failed steps within each job (max 3 per job)
- Links to each failed job

**Handles**:
- Failed jobs
- Cancelled jobs
- Timed out jobs
- Action required status

### `latest_runs_by_workflow(runs: list[dict]) -> list[dict]`
Keeps only the newest run for each workflow before evaluating CI.

This prevents an older failed run for the same commit and workflow from hiding a newer successful rerun.

### `evaluate_ci(head_sha: str) -> CiResult`
Main function - determines overall CI status for a commit.

**Status Flow**:
1. **No runs**: Returns "waiting" - CI hasn't started
2. **Active runs**: Returns "running" with first active workflow URL
3. **Failed runs**: Returns "failed" with detailed job summary
4. **Successful runs**: Returns "passed" with workflow names
5. **Other state**: Returns "waiting" - status not final yet

**Active statuses checked**: queued, requested, waiting, pending, in_progress

**Successful conclusions**: success, skipped, neutral

Before applying the status flow, `evaluate_ci()` filters workflow runs through `latest_runs_by_workflow()`.

## Configuration Dependencies
- `GITHUB_REPOSITORY`: Target repository in owner/repo format

## API Endpoints Used
- `GET /repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=20`
- `GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs`

## Usage Example
```python
from ai_agent.ci import evaluate_ci

result = evaluate_ci("abc123def456")
print(f"Status: {result.state}")
print(f"Summary: {result.summary}")
if result.url:
    print(f"Details: {result.url}")
```
