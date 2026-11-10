"""Application-observed, fixed verification profiles; never model-reported success."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_agent.context_models import AttemptSnapshot
    from ai_agent.plan_document import PlanDocument

from ai_agent.claude_env import provider_env
from ai_agent.config import WORKER_ISOLATION_ENABLED
from ai_agent.context_attempts import current_attempt
from ai_agent.context_sources import ContextBlockedError
from ai_agent.git_recovery import guarded_run
from ai_agent.projects import active_project
from ai_agent.run_outcomes import NoCodeOutcomeError, blocked_report
from ai_agent.shell import run

_PACKAGES = {
    "ai-coding-agent": "ai_agent",
    "ai-pm-agent": "ai_pm_agent",
    "ai-ops-agent": "ai_ops_agent",
    "ai-dashboard": "ai_dashboard",
}


def commands_for(profile: str) -> tuple[tuple[str, ...], ...]:
    """Versioned profiles contain argv only; plans cannot supply shell commands."""
    repository, separator, version = profile.partition(":")
    if separator != ":" or version != "baseline-v1" or repository not in _PACKAGES:
        raise ValueError("Unsupported verification profile; revise the plan")
    package = _PACKAGES[repository]
    paths = (
        ("agent.py", package, "tests", "deploy/claude-code-statusline.py")
        if repository == "ai-coding-agent"
        else (package, "tests")
    )
    commands = (
        ("ruff", "check", *paths),
        ("ruff", "format", "--check", *paths),
        ("python", "-m", "pytest", "-q"),
    )
    return commands + (("npm", "test"),) if repository == "ai-dashboard" else commands


def profile_outline(profile: str) -> list[str]:
    """Display fixed checks before owner approval, including on the dashboard."""
    if not profile:
        return []
    try:
        commands = commands_for(profile)
    except ValueError:
        return ["Unsupported verification profile; revise before approval."]
    return [
        "Verification only: application runs existing checks; no model or PR.",
        *[" ".join(command) for command in commands],
    ]


def approved_profile(document: PlanDocument, identity: dict | None) -> str:
    """Require explicit, repository-scoped verification in the approved plan."""
    profile = document.verification_profile
    if not profile:
        return ""
    commands_for(profile)
    name = (identity or {}).get("project_name")
    if (
        profile != f"{name}:baseline-v1"
        or document.files
        or [row["name"] for row in document.repositories] != [name]
        or not any(
            row["name"] == name and row["role"] == "verify"
            for row in document.repository_accounting
        )
    ):
        raise ValueError(
            "Verification profile requires one matching repository, "
            "role verify and no file edits"
        )
    return profile


def _clean(path: Path, head: str, modules: Path | None = None) -> bool:
    status = ["git", "status", "--porcelain"]
    if modules is not None:
        link = path / "node_modules"
        if not link.is_symlink() or link.resolve() != modules.resolve():
            return False
        status += ["--", ".", ":(exclude)node_modules"]
    return (
        run(["git", "rev-parse", "HEAD"], cwd=path).output.strip() == head
        and not run(status, cwd=path).output.strip()
    )


@guarded_run
def run_verification(profile: str) -> None:
    """Run fixed checks in a disposable worktree and persist their exact results."""
    from ai_agent.isolation import _prepare_ownership, sandbox_for
    from ai_agent.provider_process import grouped_run

    attempt = current_attempt.get()
    project = active_project()
    commands = commands_for(profile)
    if attempt is None or profile != f"{project.name}:baseline-v1":
        raise ContextBlockedError(
            "Verification requires the approved task's repository"
        )
    if asdict(attempt.request.scope.repository) != {
        "project_name": project.name,
        "repo_path": str(project.repo_path),
        "github_repository": project.github_repository,
    }:
        raise ContextBlockedError("Verification repository identity changed")
    remote = (
        run(["git", "remote", "get-url", "origin"]).output.strip().removesuffix(".git")
    )
    if remote not in {
        f"git@github.com:{project.github_repository}",
        f"https://github.com/{project.github_repository}",
    }:
        raise ContextBlockedError("Verification checkout remote identity mismatch")
    run(["git", "checkout", "--no-overwrite-ignore", project.base_branch])
    run(["git", "pull", "--ff-only", "origin", project.base_branch])
    head = run(["git", "rev-parse", "HEAD"]).output.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", head) or not _clean(project.repo_path, head):
        raise ContextBlockedError("Verification requires a clean checkout")
    results = []
    with sandbox_for(
        project.repo_path, attempt.request.task_id, attempt.state["branch"]
    ) as sandbox:
        run(["git", "submodule", "update", "--init", "--recursive"], cwd=sandbox.path)
        modules = None
        if project.name == "ai-dashboard":
            modules = project.repo_path / "node_modules"
            if not modules.is_dir():
                raise NoCodeOutcomeError(
                    blocked_report(
                        "Dashboard development dependencies are missing.",
                        next_step="Install development dependencies through Ops.",
                    )
                )
            (sandbox.path / "node_modules").symlink_to(
                modules, target_is_directory=True
            )
        _prepare_ownership(sandbox.path)
        for command in commands:
            argv = [
                sys.executable if value == "python" and index == 0 else value
                for index, value in enumerate(command)
            ]
            try:
                result = grouped_run(
                    argv,
                    cwd=sandbox.path,
                    env=provider_env(worker=WORKER_ISOLATION_ENABLED),
                    isolated=WORKER_ISOLATION_ENABLED,
                    timeout=1800,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    max_output_chars=8192,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise NoCodeOutcomeError(
                    blocked_report(
                        f"Could not run {' '.join(command)} ({type(error).__name__}).",
                        next_step="Check the worker environment, then retry.",
                    )
                ) from error
            results.append({"command": list(command), "exit_code": result.returncode})
            attempt.checkpoint(
                "verification_check", {"head": head, **results[-1]}, required=True
            )
            if result.returncode != 0:
                # Keep bounded diagnostics in the redacted report, not the receipt.
                raise NoCodeOutcomeError(
                    blocked_report(
                        f"Failed: {' '.join(command)} (exit {result.returncode}).",
                        evidence=result.stdout or result.stderr or "No output.",
                        next_step="Fix the failing check before retrying verification.",
                    )
                )
        if not _clean(sandbox.path, head, modules) or not _clean(
            project.repo_path, head
        ):
            raise ContextBlockedError(
                "Checkout changed during verification; completion refused"
            )
    attempt.checkpoint(
        "verification",
        {
            "schema": 1,
            "profile": profile,
            "head": head,
            "task_id": attempt.request.task_id,
            "attempt_id": attempt.state["attempt_id"],
            "checks": results,
            "clean": True,
            "observed_at": time.time(),
        },
        required=True,
    )


def verify_receipt(record: AttemptSnapshot) -> str:
    """Recheck immutable application evidence against the current clean checkout."""
    from ai_agent.dependency_evidence import git

    evidence = [
        item
        for item in record.evidence
        if item.source == "test" and item.locator == "verification:v1"
    ]
    if record.outcome != "succeeded" or len(evidence) != 1:
        raise ValueError("Successful application verification receipt unavailable")
    receipt = json.loads(evidence[0].summary)
    profile = f"{record.scope.repository.project_name}:baseline-v1"
    expected = [
        {"command": list(command), "exit_code": 0} for command in commands_for(profile)
    ]
    if (
        receipt.get("schema") != 1
        or receipt.get("profile") != profile
        or receipt.get("task_id") != record.task_id
        or receipt.get("attempt_id") != record.attempt_id
        or receipt.get("clean") is not True
        or receipt.get("checks") != expected
        or not re.fullmatch(r"[a-f0-9]{40}", str(receipt.get("head")))
        or not isinstance(receipt.get("observed_at"), (int, float))
        or not 0 < receipt["observed_at"] <= time.time()
    ):
        raise ValueError("Invalid application verification receipt")
    repository = record.scope.repository
    remote = git(repository.repo_path, "remote", "get-url", "origin").removesuffix(
        ".git"
    )
    if remote not in {
        f"git@github.com:{repository.github_repository}",
        f"https://github.com/{repository.github_repository}",
    }:
        raise ValueError("Verification checkout repository identity mismatch")
    head = git(repository.repo_path, "rev-parse", "HEAD")
    if head != receipt["head"] or git(repository.repo_path, "status", "--porcelain"):
        raise ValueError("Checkout changed since verification; rerun the checks")
    return head
