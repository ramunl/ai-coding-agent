"""Advance stale prerequisite checkouts without discarding unfinished work."""

import asyncio
import os
import re
import subprocess
from dataclasses import asdict

from ai_agent.context_models import RepositoryIdentity
from ai_agent.projects import get_project

_SYNC_LOCK = asyncio.Lock()


class StaleCheckoutError(ValueError):
    """The merged prerequisite is absent from the captured checkout."""

    def __init__(self, repository: RepositoryIdentity, commit: str) -> None:
        """Retain the captured identity and authoritative merge commit."""
        super().__init__("Planner checkout is stale; merged prerequisite is absent")
        self.repository = repository
        self.commit = commit


def _git(path: str, *args: str, timeout: int = 5) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("Planner checkout synchronization timed out; retry") from error
    if result.returncode:
        messages = {
            "fetch": "Planner checkout is stale; fetch failed; check Git access",
            "merge": "Planner checkout has local divergence; fast-forward refused",
            "merge-base": "Fetched base branch lacks the verified prerequisite merge",
        }
        raise ValueError(messages.get(args[0], "Planner checkout evidence unavailable"))
    return result.stdout.strip()


def _idle(data: dict) -> None:
    if any(
        data.get(key)
        for key in (
            "active_execution",
            "queue_runner_active",
            "task_queue",
            "planning_active",
            "planning_task",
            "thinking",
            "pending_plan",
            "pending_implementation",
            "pending_bugfix_clarification",
            "context_recovery_pending",
        )
    ):
        raise ValueError(
            "Planner checkout is stale; finish active or pending work first"
        )


def _state(repository: RepositoryIdentity, branch: str) -> tuple[str, str]:
    path = repository.repo_path
    remote = _git(path, "remote", "get-url", "origin")
    if remote.removesuffix(".git") not in {
        f"https://github.com/{repository.github_repository}",
        f"git@github.com:{repository.github_repository}",
    }:
        raise ValueError("Planner checkout repository identity changed; review it")
    if _git(path, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        raise ValueError("Planner checkout is stale; switch to its base branch first")
    if _git(path, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError(
            "Planner checkout has unfinished edits; preserve or finish them first"
        )
    return _git(path, "rev-parse", "HEAD"), remote


def _registered_branch(repository: RepositoryIdentity) -> str:
    project = get_project(repository.project_name)
    identity = {
        "project_name": project.name,
        "repo_path": str(project.repo_path),
        "github_repository": project.github_repository,
    }
    if asdict(repository) != identity:
        raise ValueError("Planner registration changed; review dependency scope")
    return project.base_branch


async def synchronize(stale: StaleCheckoutError, data: dict) -> None:
    """Fetch off-loop, then fast-forward an unchanged clean idle base branch.

    Final checks and merge have no await: another bot handler cannot start
    work between the idle check and the checkout change. No reset, stash,
    cleanup or branch switch is performed.
    """
    async with _SYNC_LOCK:
        _idle(data)
        repository = stale.repository
        if not re.fullmatch(r"[a-f0-9]{40}", stale.commit):
            raise ValueError("Invalid prerequisite merge commit")
        branch = _registered_branch(repository)
        _git(repository.repo_path, "check-ref-format", "refs/heads/" + branch)
        before = _state(repository, branch)
        await asyncio.to_thread(
            _git,
            repository.repo_path,
            "fetch",
            "--no-tags",
            "origin",
            f"refs/heads/{branch}:refs/remotes/origin/{branch}",
            timeout=30,
        )
        _idle(data)
        if _registered_branch(repository) != branch:
            raise ValueError("Planner base branch changed during fetch; retry")
        if _state(repository, branch) != before:
            raise ValueError(
                "Planner checkout changed during fetch; retry verification"
            )
        target = "refs/remotes/origin/" + branch
        _git(repository.repo_path, "merge-base", "--is-ancestor", stale.commit, target)
        _git(repository.repo_path, "merge", "--ff-only", "--no-edit", target)
