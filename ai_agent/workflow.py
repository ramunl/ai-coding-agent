import re
from dataclasses import dataclass

from ai_agent.config import CLAUDE_CODE_ARGS, CODEX_TIMEOUT_SECONDS, IMPLEMENTATION_AGENT
from ai_agent.projects import active_project
from ai_agent.github import PullRequest, ensure_github_configured, github_request
from ai_agent.shell import run


@dataclass(frozen=True)
class ImplementationResult:
    output: str
    files_changed: list[str]
    diff: str


SUPPORTED_IMPLEMENTATION_AGENTS = ("codex", "claude")


def normalize_implementation_agent(agent: str | None = None) -> str:
    value = (agent or IMPLEMENTATION_AGENT).strip().lower()
    if value not in SUPPORTED_IMPLEMENTATION_AGENTS:
        raise ValueError(f"Unsupported implementation agent: {value}. Use codex or claude.")
    return value


def implementation_agent_label(agent: str | None = None) -> str:
    return {"codex": "Codex", "claude": "Claude"}[normalize_implementation_agent(agent)]


def implementation_command(prompt: str, agent: str | None = None) -> list[str]:
    selected_agent = normalize_implementation_agent(agent)
    if selected_agent == "claude":
        return ["claude", "-p", prompt, *CLAUDE_CODE_ARGS]
    return ["codex", "exec", prompt]


def run_implementation_agent(prompt: str, agent: str | None = None):
    return run(implementation_command(prompt, agent), timeout=CODEX_TIMEOUT_SECONDS)


def slugify_branch_name(change_description: str, prefix: str = "feature") -> str:
    validate_branch_prefix(prefix)
    slug = change_description.lower().strip()
    slug = re.sub(r"[/\\:?*\[\]().]+", "-", slug)
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-./")
    if not slug:
        slug = "change"
    branch_name = f"{prefix}/{slug[:80].strip('-./')}"
    validate_branch_name(branch_name)
    return branch_name


def validate_branch_prefix(prefix: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", prefix):
        raise ValueError(f"Invalid branch prefix: {prefix}")


def validate_branch_name(branch_name: str) -> None:
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


def implement(plan: str, branch_name: str, agent: str | None = None) -> ImplementationResult:
    validate_branch_name(branch_name)
    base_branch = active_project().base_branch
    run(["git", "checkout", base_branch])
    run(["git", "pull", "origin", base_branch])
    run(["git", "checkout", "-b", branch_name])
    agent_result = run_implementation_agent(plan, agent)
    run(["git", "add", "-N", "."])
    files_changed = changed_files()
    diff = run(["git", "diff", "--no-ext-diff"]).output
    return ImplementationResult(output=agent_result.output, files_changed=files_changed, diff=diff)


def repair_implementation(prompt: str, branch_name: str, agent: str | None = None) -> ImplementationResult:
    validate_branch_name(branch_name)
    run(["git", "checkout", branch_name])
    run(["git", "pull", "origin", branch_name])
    agent_result = run_implementation_agent(prompt, agent)
    run(["git", "add", "-N", "."])
    files_changed = changed_files()
    diff = run(["git", "diff", "--no-ext-diff"]).output
    return ImplementationResult(output=agent_result.output, files_changed=files_changed, diff=diff)


def repair_pull_request_branch(prompt: str, branch_name: str, agent: str | None = None) -> ImplementationResult:
    validate_branch_name(branch_name)
    run(["git", "fetch", "origin", branch_name])
    run(["git", "checkout", "-B", branch_name, f"origin/{branch_name}"])
    agent_result = run_implementation_agent(prompt, agent)
    run(["git", "add", "-N", "."])
    files_changed = changed_files()
    diff = run(["git", "diff", "--no-ext-diff"]).output
    return ImplementationResult(output=agent_result.output, files_changed=files_changed, diff=diff)


def changed_files() -> list[str]:
    output = run(["git", "status", "--porcelain"]).output
    names = [line[3:].strip() for line in output.splitlines() if len(line) > 3]
    return sorted(set(names))


def has_changes() -> bool:
    return bool(run(["git", "status", "--porcelain"]).output.strip())


def push(branch_name: str, change_name: str, commit_type: str = "feat") -> str:
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
    ensure_github_configured()
    validate_branch_name(branch_name)
    payload = {
        "title": f"{title_type}: {change_name}",
        "head": branch_name,
        "base": active_project().base_branch,
        "body": f"Generated by Channel Cast Agent.\n\n{body_label}:\n\n{body_text}",
        "maintainer_can_modify": True,
    }
    response = github_request("POST", f"/repos/{active_project().github_repository}/pulls", payload)
    return PullRequest(
        number=int(response["number"]),
        url=response["html_url"],
        head_sha=response["head"]["sha"],
    )
