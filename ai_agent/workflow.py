"""Implement changes and publish their branches and pull requests."""

import os
import re
from dataclasses import dataclass

from ai_agent.config import (
    CLAUDE_CODE_ARGS,
    CODEX_TIMEOUT_SECONDS,
    IMPLEMENTATION_AGENT,
)
from ai_agent.github import PullRequest, ensure_github_configured, github_request
from ai_agent.model_errors import codex_capacity_explained
from ai_agent.projects import active_project
from ai_agent.shell import CommandResult, run


@dataclass(frozen=True)
class ImplementationResult:
    """Capture agent output, changed paths, and the resulting diff."""

    output: str
    files_changed: list[str]
    diff: str


SUPPORTED_IMPLEMENTATION_AGENTS = ("codex", "claude")


def normalize_implementation_agent(agent: str | None = None) -> str:
    """Resolve an implementation provider or reject an unsupported name."""
    value = (agent or IMPLEMENTATION_AGENT).strip().lower()
    if value not in SUPPORTED_IMPLEMENTATION_AGENTS:
        raise ValueError(
            f"Unsupported implementation agent: {value}. Use codex or claude."
        )
    return value


def implementation_agent_label(agent: str | None = None) -> str:
    """Return the display name for an implementation provider."""
    return {"codex": "Codex", "claude": "Claude"}[normalize_implementation_agent(agent)]


def safe_claude_code_args() -> list[str]:
    """Adapt configured CLI arguments for execution as root."""
    args = list(CLAUDE_CODE_ARGS)
    if os.geteuid() != 0:
        return args

    safe_args: list[str] = []
    skip_next = False
    for index, value in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if value == "--dangerously-skip-permissions":
            continue
        if (
            value == "--permission-mode"
            and index + 1 < len(args)
            and args[index + 1] == "bypassPermissions"
        ):
            safe_args.extend(["--permission-mode", "acceptEdits"])
            skip_next = True
            continue
        safe_args.append(value)
    return safe_args


def implementation_command(prompt: str, agent: str | None = None) -> list[str]:
    """Build the selected provider command for an implementation prompt."""
    selected_agent = normalize_implementation_agent(agent)
    if selected_agent == "claude":
        return ["claude", "-p", prompt, *safe_claude_code_args()]
    # Codex has no TTY to answer a per-directory trust prompt when run non-
    # interactively,
    # so it silently falls back to a read-only sandbox for any repo path that isn't
    # already
    # marked trusted in ~/.codex/config.toml, producing "no file changes" with no error.
    # Requesting workspace-write explicitly makes writes work regardless of ambient
    # trust.
    return ["codex", "exec", "-s", "workspace-write", prompt]


def run_implementation_agent(prompt: str, agent: str | None = None) -> CommandResult:
    """Run a provider command and explain capacity failures."""
    with codex_capacity_explained():
        return run(implementation_command(prompt, agent), timeout=CODEX_TIMEOUT_SECONDS)


def slugify_branch_name(
    change_description: str, prefix: str = "feature", max_slug_length: int = 20
) -> str:
    """Build a valid, bounded branch name from a change description."""
    validate_branch_prefix(prefix)
    slug = change_description.lower().strip()
    slug = re.sub(r"[/\\:?*\[\]().]+", "-", slug)
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-./")
    if not slug:
        slug = "change"
    slug = truncate_slug(slug, max_slug_length)
    branch_name = f"{prefix}/{slug}"
    validate_branch_name(branch_name)
    return branch_name


def truncate_slug(slug: str, max_length: int) -> str:
    """Shorten a branch slug, preferring a word boundary."""
    if len(slug) <= max_length:
        return slug
    truncated = slug[:max_length]
    # Prefer cutting at a word boundary so we don't leave a chopped-off
    # fragment like "...slash-commands-im" (from "...commands-immediately").
    last_hyphen = truncated.rfind("-")
    if last_hyphen > 0:
        truncated = truncated[:last_hyphen]
    return truncated.strip("-./")


def validate_branch_prefix(prefix: str) -> None:
    """Reject prefixes containing characters outside the allowed set."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", prefix):
        raise ValueError(f"Invalid branch prefix: {prefix}")


def validate_branch_name(branch_name: str) -> None:
    """Reject unsupported branch names before invoking Git."""
    invalid = (
        branch_name.startswith("/")
        or branch_name.endswith("/")
        or branch_name.endswith(".")
        or ".." in branch_name
        or "@{" in branch_name
        or "\\" in branch_name
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch_name)
    )
    if invalid:
        raise ValueError(f"Invalid branch name: {branch_name}")


def implement(
    plan: str, branch_name: str, agent: str | None = None
) -> ImplementationResult:
    """Create a feature branch and capture the implementation result."""
    validate_branch_name(branch_name)
    base_branch = active_project().base_branch
    run(["git", "checkout", base_branch])
    run(["git", "pull", "origin", base_branch])
    run(["git", "checkout", "-b", branch_name])
    return _run_and_capture_changes(plan, agent)


def repair_implementation(
    prompt: str, branch_name: str, agent: str | None = None
) -> ImplementationResult:
    """Update an existing branch and capture a repair result."""
    validate_branch_name(branch_name)
    run(["git", "checkout", branch_name])
    run(["git", "pull", "origin", branch_name])
    return _run_and_capture_changes(prompt, agent)


def repair_pull_request_branch(
    prompt: str, branch_name: str, agent: str | None = None
) -> ImplementationResult:
    """Check out a remote pull-request branch and capture its repair result."""
    validate_branch_name(branch_name)
    run(["git", "fetch", "origin", branch_name])
    run(["git", "checkout", "-B", branch_name, f"origin/{branch_name}"])
    return _run_and_capture_changes(prompt, agent)


def _run_and_capture_changes(prompt: str, agent: str | None) -> ImplementationResult:
    """Capture provider output and the resulting Git changes consistently."""
    agent_result = run_implementation_agent(prompt, agent)
    run(["git", "add", "-N", "."])
    files_changed = changed_files()
    diff = run(["git", "diff", "--no-ext-diff"]).output
    return ImplementationResult(
        output=agent_result.output, files_changed=files_changed, diff=diff
    )


def return_to_base_branch() -> None:
    """Check out and update the active project base branch."""
    base_branch = active_project().base_branch
    run(["git", "checkout", base_branch])
    run(["git", "pull", "origin", base_branch])


def changed_files() -> list[str]:
    """Return unique paths reported by Git status."""
    output = run(["git", "status", "--porcelain"]).output
    names = [line[3:].strip() for line in output.splitlines() if len(line) > 3]
    return sorted(set(names))


def has_changes() -> bool:
    """Check whether the active repository has uncommitted changes."""
    return bool(run(["git", "status", "--porcelain"]).output.strip())


def push(branch_name: str, change_name: str, commit_type: str = "feat") -> str:
    """Commit and push changes, returning the resulting commit SHA."""
    validate_branch_name(branch_name)
    if not has_changes():
        raise RuntimeError(
            "The implementation agent finished but made no file changes, so "
            "there is nothing to commit. This usually means the task was too "
            "vague to act on or the agent described the change instead of "
            "making it. Try /discuss to sharpen the plan, or re-run with a "
            "more specific feature description."
        )
    run(["git", "add", "."])
    run(["git", "commit", "-m", f"{commit_type}: {change_name}"])
    run(["git", "push", "origin", branch_name])
    return run(["git", "rev-parse", "HEAD"]).output.strip()


def create_pull_request(
    branch_name: str,
    change_name: str,
    body_text: str,
    title_type: str = "feat",
    body_label: str = "Plan",
) -> PullRequest:
    """Open a pull request against the active project base branch."""
    ensure_github_configured()
    validate_branch_name(branch_name)
    payload = {
        "title": f"{title_type}: {change_name}",
        "head": branch_name,
        "base": active_project().base_branch,
        "body": f"Generated by Channel Cast Agent.\n\n{body_label}:\n\n{body_text}",
        "maintainer_can_modify": True,
    }
    response = github_request(
        "POST", f"/repos/{active_project().github_repository}/pulls", payload
    )
    return PullRequest(
        number=int(response["number"]),
        url=response["html_url"],
        head_sha=response["head"]["sha"],
    )
