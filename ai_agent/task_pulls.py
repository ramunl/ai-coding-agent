"""Follow each task's pull request on GitHub: open, merged or closed.

Every few minutes this asks GitHub about the branch of each task in the "pr"
or "ended" stage: merged -> "done", closed or confirmed absent -> "stopped",
open -> "pr". Failed or malformed lookups leave the task unchanged.
Read-only requests.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import MutableMapping
from datetime import datetime
from typing import Any

from ai_agent.github import github_request
from ai_agent.projects import get_project
from ai_agent.tasks import set_stage, tasks_of

logger = logging.getLogger(__name__)

POLL_SECONDS = 300
# A run that just ended unseen is settled sooner: the owner is likely watching.
ENDED_POLL_SECONDS = 30


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
    if not isinstance(pulls, list):
        raise ValueError("Invalid GitHub pull request list")
    if not pulls:
        return None
    pull = pulls[0]  # GitHub lists the newest first
    if not isinstance(pull, dict) or pull.get("state") not in ("open", "closed"):
        raise ValueError("Invalid GitHub pull request state")
    if not isinstance(pull.get("html_url"), str) or not pull["html_url"]:
        raise ValueError("Invalid GitHub pull request URL")
    merged_at = _epoch(pull.get("merged_at"))
    if pull.get("merged_at") is not None and merged_at is None:
        raise ValueError("Invalid GitHub pull request merge timestamp")
    return {
        "url": pull.get("html_url"),
        "head_sha": (pull.get("head") or {}).get("sha"),
        "state": "merged" if merged_at is not None else pull.get("state"),
        "merged_at": merged_at,
        "merge_commit": pull.get("merge_commit_sha") if merged_at is not None else None,
    }


def apply_pull(task: dict, pull: dict | None) -> bool:
    """Move a task by its pull request's state; True if anything changed."""
    before = dict(task)
    if pull is None:
        if task.get("stage") not in ("pr", "ended"):
            return False
        for key in ("pr_url", "merged_at", "merge_commit"):
            task.pop(key, None)
        set_stage(
            task,
            "stopped",
            "the run ended without a pull request; GitHub confirmed none",
        )
        return True
    if not isinstance(pull, dict) or pull.get("state") not in (
        "open",
        "closed",
        "merged",
    ):
        raise ValueError("Invalid pull request state")
    if pull.get("url"):
        task["pr_url"] = pull["url"]
    if pull["state"] == "merged":
        task["merged_at"] = pull.get("merged_at")
        task["merge_commit"] = pull.get("merge_commit")
        set_stage(task, "done")
    elif pull["state"] == "closed":
        set_stage(task, "stopped", "the pull request was closed without merging")
    elif pull["state"] == "open" and task.get("stage") == "ended":
        set_stage(task, "pr")
    return task != before


def attempt_pull(task: dict, repository: str, pull: dict | None) -> dict | None:
    """Bind modern task tracking to archived work, never just a reused branch."""
    if pull is None or not task.get("context_handoff"):
        return pull
    from ai_agent.config import CHAT_ID
    from ai_agent.context_models import AttemptSnapshot
    from ai_agent.context_store import ContextStore
    from ai_agent.context_validation import decode_dependency

    ref = task["context_handoff"]
    dependency = decode_dependency(
        {"task_id": task["id"], "scope": ref["scope"], "attempt": ref},
        str(CHAT_ID),
    )
    if dependency.scope.repository.github_repository != repository:
        raise ValueError("Attempt repository mismatch")
    record = ContextStore(dependency.scope).get(
        "attempt", dependency.attempt.record_id, dependency.attempt.revision
    )
    if not isinstance(record, AttemptSnapshot) or record.task_id != task["id"]:
        raise ValueError("Attempt task identity mismatch")
    commits = {e.locator for e in record.evidence if e.source == "commit"}
    urls = {e.locator for e in record.evidence if e.source == "pull_request"}
    if pull.get("head_sha") not in commits or (urls and pull["url"] not in urls):
        return None
    return pull


async def check_once(data: MutableMapping) -> bool:
    """Ask GitHub about every task waiting on its pull request; True if any moved."""
    changed = False
    for task, identity in [
        (t, (t.get("attempt", 1), t["branch"], t["stage"]))
        for t in tasks_of(data)
        if t.get("stage") in ("pr", "ended") and t.get("branch")
    ]:
        try:
            repository = (
                await asyncio.to_thread(get_project, task["repo"])
            ).github_repository
            pull = await asyncio.to_thread(pull_state, repository, identity[1])
            pull = await asyncio.to_thread(attempt_pull, task, repository, pull)
        except Exception as error:  # one repo failing must not stop the others
            logger.warning(
                "Task %s: pull request check failed: %s", task.get("id"), error
            )
            continue
        if not any(t is task for t in tasks_of(data)) or identity != (
            task.get("attempt", 1),
            task.get("branch"),
            task.get("stage"),
        ):
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
        data = ptb_app.user_data.get(owner_id) or {}
        waiting = any(task.get("stage") == "ended" for task in data.get("tasks") or [])
        await asyncio.sleep(min(interval, ENDED_POLL_SECONDS) if waiting else interval)


def verified_pull(repository: str, url: str, head_sha: str) -> dict:
    """Fresh exact-PR facts; branch names and cached display stages are insufficient.

    Reviewable means open, explicitly non-draft, and bound to the archived
    attempt head in the captured repository. It does not claim CI or approval.
    """
    match = re.fullmatch(
        r"https://github\.com/" + re.escape(repository) + r"/pull/([1-9][0-9]*)", url
    )
    if not match or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ValueError("Missing identity-bound PR evidence")
    number = int(match[1])
    pull = github_request(
        "GET", f"/repos/{repository}/pulls/{number}", timeout=10, max_bytes=256 * 1024
    )
    if (
        pull.get("number") != number
        or pull.get("html_url") != url
        or pull.get("base", {}).get("repo", {}).get("full_name") != repository
        or pull.get("head", {}).get("repo", {}).get("full_name") != repository
        or pull.get("head", {}).get("sha") != head_sha
        or pull.get("state") not in {"open", "closed"}
        or type(pull.get("draft")) is not bool
        or type(pull.get("merged")) is not bool
    ):
        raise ValueError("PR identity or state is incomplete")
    merged_at = _epoch(pull.get("merged_at"))
    merged = pull["merged"] and pull["state"] == "closed" and merged_at is not None
    if pull["merged"] != (merged_at is not None) or (
        pull["merged"] and (not merged or merged_at > time.time())
    ):
        raise ValueError("Invalid merge evidence")
    return {
        "reviewable": pull["state"] == "open"
        and not pull["draft"]
        and not pull["merged"],
        "merged": merged,
        "merge_commit": pull.get("merge_commit_sha"),
        "head": head_sha,
        "url": url,
        "checked_at": time.time(),
    }
