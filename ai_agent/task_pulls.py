"""Follow each task's pull request on GitHub: open, merged or closed.

The agent records a pull request when a task's run finishes; what happens to
it afterwards (review, merge) happens on GitHub. Every few minutes this asks
GitHub about the branch of each task in the "pr" stage and moves the task:
merged -> "done", closed without merging -> "stopped". Read-only requests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import MutableMapping
from datetime import datetime
from typing import Any

from ai_agent.github import github_request
from ai_agent.projects import get_project
from ai_agent.tasks import set_stage, tasks_of

logger = logging.getLogger(__name__)

POLL_SECONDS = 300


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pull_state(repository: str, branch: str) -> dict | None:
    """The newest pull request from branch in repository, or None if there is none."""
    owner = repository.split("/", 1)[0]
    pulls = github_request(
        "GET",
        f"/repos/{repository}/pulls",
        query={"head": f"{owner}:{branch}", "state": "all", "per_page": "5"},
    )
    if not isinstance(pulls, list) or not pulls:
        return None
    pull = pulls[0]  # GitHub lists the newest first
    merged_at = _epoch(pull.get("merged_at"))
    return {
        "url": pull.get("html_url"),
        "state": "merged" if merged_at else pull.get("state"),
        "merged_at": merged_at,
        "merge_commit": pull.get("merge_commit_sha") if merged_at else None,
    }


def apply_pull(task: dict, pull: dict | None) -> bool:
    """Move a task by its pull request's state; True if anything changed."""
    if pull is None:
        return False
    before = dict(task)
    if pull.get("url"):
        task["pr_url"] = pull["url"]
    if pull["state"] == "merged":
        task["merged_at"] = pull.get("merged_at")
        task["merge_commit"] = pull.get("merge_commit")
        set_stage(task, "done")
    elif pull["state"] == "closed":
        set_stage(task, "stopped", "the pull request was closed without merging")
    elif task.get("note") == "run finished; pull request not recorded":
        set_stage(task, "pr")
    return task != before


async def check_once(data: MutableMapping) -> bool:
    """Ask GitHub about every task waiting on its pull request; True if any moved."""
    changed = False
    for task in [
        t for t in tasks_of(data) if t.get("stage") == "pr" and t.get("branch")
    ]:
        try:
            repository = (
                await asyncio.to_thread(get_project, task["repo"])
            ).github_repository
            pull = await asyncio.to_thread(pull_state, repository, task["branch"])
        except Exception as error:  # one repo failing must not stop the others
            logger.warning(
                "Task %s: pull request check failed: %s", task.get("id"), error
            )
            continue
        changed |= apply_pull(task, pull)
    return changed


async def track_forever(
    ptb_app: Any, owner_id: int, interval: float = POLL_SECONDS
) -> None:
    """Check pull requests until cancelled; a failing pass is logged and retried."""
    while True:
        try:
            data = ptb_app.user_data.get(owner_id)
            if data is not None and await check_once(data):
                ptb_app.mark_data_for_update_persistence(user_ids=owner_id)
        except Exception as error:
            logger.warning("Pull request tracking failed (will retry): %s", error)
        await asyncio.sleep(interval)
