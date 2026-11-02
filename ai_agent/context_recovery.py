"""Read-only reconciliation of interrupted effects; absence is not success."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from ai_agent.context_attempts import Attempt, unfinished
from ai_agent.context_models import Scope
from ai_agent.context_store import ContextStore


def detect_pending(owner_id: int) -> bool:
    """Use authoritative journals on restart, including attempts missing from PTB."""
    store = ContextStore(Scope(str(owner_id)))
    if not store.root.exists():
        return False
    return bool(unfinished(store))


@contextmanager
def _recovery_lease(identity: dict) -> Iterator[Path]:
    """Validate the registration and exclude live executions before observation."""
    from ai_agent.projects import get_project
    from ai_agent.provider_process import require_no_writer
    from ai_agent.shell import run

    project = get_project(identity["project_name"])
    if (str(project.repo_path), project.github_repository) != (
        identity["repo_path"],
        identity["github_repository"],
    ):
        raise RuntimeError("Captured repository registration changed")
    root = project.repo_path
    common = run(["git", "rev-parse", "--git-common-dir"], cwd=root).output.strip()
    lock_path = (root / common).resolve() / "ai-agent-run.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_no_writer(root)
        yield root
    finally:
        os.close(descriptor)


def _restore_attempt(state: dict, data: dict) -> Attempt:
    attempt = Attempt(state["spec"], state["branch"], data, recovering=True)
    attempt.state = {key: value for key, value in state.items() if key != "journal"}
    attempt.name = state["journal"]
    attempt.request = replace(attempt.request, attempt_id=state["attempt_id"])
    return attempt


def _observe(state: dict, data: dict, root: Path) -> None:
    """Observe only captured repository state; never retry an external effect."""
    from ai_agent.ci import evaluate_ci
    from ai_agent.github import github_request
    from ai_agent.shell import run

    spec = state["spec"]
    identity = spec["scope"]["repository"]
    branch = state["branch"]
    stages = {entry["stage"] for entry in state["events"].values()}
    effect_intents = {
        "implementation_intent",
        "commit_intent",
        "push_intent",
        "pr_intent",
    }
    if not stages.intersection(effect_intents | {"pr", "push"}):
        # Crash before any provider/publication operation: no external effect
        # was authorized. Even an absent branch is an incomplete attempt.
        attempt = _restore_attempt(state, data)
        attempt.checkpoint("interrupted", {"state": "before external execution"})
        attempt.finish("failed")
        return
    if not branch:
        return
    repository = identity["github_repository"]
    sha = run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=root
    ).output.strip()
    pulls = github_request(
        "GET",
        f"/repos/{repository}/pulls",
        query={
            "state": "all",
            "head": f"{repository.split('/')[0]}:{branch}",
            "per_page": 100,
        },
    )
    if not isinstance(pulls, list) or len(pulls) >= 100:
        return
    matches = [
        pr
        for pr in pulls
        if (pr.get("head") or {}).get("sha") == sha
        and (pr.get("head") or {}).get("ref") == branch
        and ((pr.get("head") or {}).get("repo") or {}).get("full_name") == repository
    ]
    attempt = _restore_attempt(state, data)
    if not matches:
        # Before a PR intent, a quiescent interrupted run is definitively
        # incomplete. Keep its local/remote work; only explicit retry may act.
        stages = {entry["stage"] for entry in state["events"].values()}
        if not pulls and not stages.intersection({"pr_intent", "pr"}):
            attempt.checkpoint("interrupted", {"sha": sha, "state": "incomplete"})
            attempt.finish("failed")
        return
    if len(matches) != 1:
        return
    pr = matches[0]
    attempt.checkpoint(
        "pr", {"number": pr["number"], "url": pr["html_url"], "sha": sha}
    )
    for task in data.get("tasks", []):
        if (
            task.get("id") == spec["task_id"]
            and task.get("repo") == identity["project_name"]
            and task.get("branch") == branch
        ):
            task["pr_url"] = pr["html_url"]
    ci = evaluate_ci(sha, repository=repository)
    attempt.checkpoint("ci", {"state": ci.state, "sha": sha, "url": ci.url})
    if ci.state in {"passed", "failed"}:
        attempt.finish("succeeded" if ci.state == "passed" else "failed")


def reconcile(data: dict, owner_id: int) -> bool:
    """Reconcile every captured registered repository without changing selection.

    No Git checkout, pull, commit, push or PR creation occurs. Unknown effects,
    busy providers, changed registration or inaccessible evidence remain pending.
    """
    store = ContextStore(Scope(str(owner_id)))
    for state in unfinished(store):
        if state.get("handoff"):
            attempt = _restore_attempt(state, data)
            attempt.finish(state["handoff"]["outcome"])
            continue
        try:
            identity = state["spec"]["scope"]["repository"]
            with _recovery_lease(identity) as root:
                _observe(state, data, root)
        except Exception:
            continue
    data["context_recovery_pending"] = bool(unfinished(store))
    return not data["context_recovery_pending"]
