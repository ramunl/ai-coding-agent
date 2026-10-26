"""Represent plan revisions and render plans and execution summaries."""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from ai_agent.config import redact_sensitive
from ai_agent.ecosystem import repository_note
from ai_agent.plan_document import PlanDocument, parse_plan_document
from ai_agent.projects import active_project

__all__ = [
    "OUTLINE_ITEMS",
    "OUTLINE_ITEM_LENGTH",
    "ExecutionState",
    "PlanDocument",
    "PlanState",
    "Verbosity",
    "capture_repository_identity",
    "new_plan_state",
    "parse_plan_document",
    "parse_verbosity",
    "plan_outline",
    "render_completion",
    "render_diff_summary",
    "render_history",
    "render_plan",
    "repository_mismatch",
    "revise_plan_state",
]


class Verbosity(StrEnum):
    """Select the amount of detail shown in bot responses."""

    CONCISE = "concise"
    NORMAL = "normal"
    DEBUG = "debug"


@dataclass
class PlanState:
    """Keep a plan revision, approval state, and previous revisions."""

    id: str
    feature: str
    revision: int
    plan_text: str
    approved: bool
    history: list[str] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    repository_identity: dict[str, str] | None = None


@dataclass(frozen=True)
class ExecutionState:
    """Capture implementation output for completion and status responses."""

    branch: str
    files_changed: list[str]
    diff_summary: str
    full_diff: str
    logs: str
    pr_url: str | None = None
    tests: str = "UNKNOWN"


def capture_repository_identity() -> dict[str, str]:
    """Snapshot the registered repository before any planning awaits."""
    project = active_project()
    return {
        "project_name": project.name,
        "repo_path": str(project.repo_path),
        "github_repository": project.github_repository,
    }


def repository_mismatch(identity: object) -> str | None:
    """Reject legacy work and any change to its original repository."""
    keys = ("project_name", "repo_path", "github_repository")
    if not isinstance(identity, dict) or any(
        not isinstance(identity.get(key), str) or not identity[key] for key in keys
    ):
        return (
            "This saved work lacks repository identity. "
            "Recreate it with /plan or /bugfix."
        )
    try:
        current = capture_repository_identity()
    except (OSError, RuntimeError, ValueError):
        return (
            "Cannot verify the original repository. Restore its registration and retry."
        )
    if any(identity[key] != current[key] for key in keys):
        return (
            f"This work belongs to {identity['project_name']} "
            f"({identity['github_repository']}, {identity['repo_path']}). "
            f"Select the original project with /use {identity['project_name']}. "
            "If its registration changed, recreate the work."
        )
    return None


def new_plan_state(
    feature: str, plan_text: str, repository_identity: dict[str, str] | None = None
) -> PlanState:
    """Create an unapproved first revision for a feature."""
    return PlanState(
        id=str(uuid.uuid4())[:8],
        feature=feature,
        revision=1,
        plan_text=plan_text,
        approved=False,
        history=[],
        repository_identity=dict(repository_identity) if repository_identity else None,
    )


def revise_plan_state(plan: PlanState, plan_text: str) -> PlanState:
    """Create an unapproved revision while preserving prior plan text."""
    return PlanState(
        id=plan.id,
        feature=plan.feature,
        revision=plan.revision + 1,
        plan_text=plan_text,
        approved=False,
        history=[*plan.history, plan.plan_text],
        images=list(plan.images),
        repository_identity=(
            dict(plan.repository_identity) if plan.repository_identity else None
        ),
    )


def parse_verbosity(value: str) -> Verbosity | None:
    """Resolve a verbosity name, returning None for unknown values."""
    normalized = value.strip().lower()
    for verbosity in Verbosity:
        if normalized == verbosity.value:
            return verbosity
    return None


def render_plan(plan: PlanState) -> str:
    """Render a plan using the requested level of detail."""
    document = parse_plan_document(plan.plan_text, plan.feature)
    identity = plan.repository_identity or {}
    project_name = identity.get("project_name", "Unknown (legacy plan; recreate)")
    github_repository = identity.get("github_repository", "unknown")
    lines = [
        f"Plan #{plan.id}",
        f"Revision: {plan.revision}",
        "",
        "Project:",
        f"{project_name} ({github_repository})",
        "",
        "Branch:",
        document.branch,
        "",
        "Summary:",
        document.summary,
    ]
    if document.repositories:
        lines.extend(["", "Repositories, in order:"])
        lines.extend(
            f"{index}. {repository['name']}: {repository['changes']}"
            for index, repository in enumerate(document.repositories, 1)
        )
        note = repository_note(document.repositories, project_name)
        if note:
            lines.append(note)
    if document.files:
        lines.extend(["", "Files:", *[f"- {file}" for file in document.files]])
    if document.steps:
        lines.extend(
            [
                "",
                "Implementation:",
                *[f"{index}. {step}" for index, step in enumerate(document.steps, 1)],
            ]
        )
    if document.risks:
        lines.extend(["", "Risks:", *[f"- {risk}" for risk in document.risks]])
    lines.extend(["", "Commands:", "- /discuss <feedback>", "- /approve", "- /cancel"])
    return "\n".join(lines)


OUTLINE_ITEMS = 30
OUTLINE_ITEM_LENGTH = 500


def plan_outline(plan: PlanState) -> dict:
    """The parts of a plan a dashboard shows, bounded in size and redacted.

    Not the full plan text: the generated prompt stays out, and a runaway plan
    cannot bloat the snapshot that is rewritten every few seconds.
    """
    document = parse_plan_document(plan.plan_text, plan.feature)

    def short(text: object) -> str:
        return redact_sensitive(str(text))[:OUTLINE_ITEM_LENGTH]

    def items(values: list) -> list[str]:
        return [short(value) for value in values[:OUTLINE_ITEMS]]

    return {
        "branch": short(document.branch),
        "summary": short(document.summary),
        "files": items(document.files),
        "steps": items(document.steps),
        "risks": items(document.risks),
        "repositories": [
            {"name": short(item["name"]), "changes": short(item["changes"])}
            for item in document.repositories
        ],
        "repository_note": short(
            repository_note(
                document.repositories,
                (plan.repository_identity or {}).get("project_name", ""),
            )
        ),
    }


def render_history(plan: PlanState) -> str:
    """Describe the revisions retained for a plan."""
    revisions = [*plan.history, plan.plan_text]
    lines = [f"Plan #{plan.id} history"]
    for index, text in enumerate(revisions, 1):
        document = parse_plan_document(text, plan.feature)
        lines.append(f"Revision {index}: {document.summary}")
    return "\n".join(lines)


def render_diff_summary(diff_text: str, files: list[str]) -> str:
    """Summarize file changes from a unified diff."""
    added = sum(
        1
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1
        for line in diff_text.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    lines = ["Modified files:"]
    if files:
        lines.extend(f"{index}. {file}" for index, file in enumerate(files, 1))
    else:
        lines.append("(none)")
    lines.extend(["", f"+{added} lines", f"-{removed} lines"])
    return "\n".join(lines)


def render_completion(execution: ExecutionState, verbosity: Verbosity) -> str:
    """Render captured execution results at the requested verbosity."""
    heading = (
        "Implementation failed."
        if execution.tests == "FAIL"
        else "Implementation completed."
    )
    lines = [
        heading,
        "",
        f"Files changed: {len(execution.files_changed)}",
        f"Tests: {execution.tests}",
    ]
    if execution.pr_url:
        lines.append(f"PR: {execution.pr_url}")
    if verbosity in {Verbosity.NORMAL, Verbosity.DEBUG} and execution.files_changed:
        lines.extend(
            ["", "Files changed:", *[f"- {file}" for file in execution.files_changed]]
        )
    if verbosity == Verbosity.DEBUG:
        if execution.full_diff:
            lines.extend(["", "Diff:", execution.full_diff])
        if execution.logs:
            lines.extend(["", "Logs:", execution.logs])
    lines.extend(
        ["", "Commands:", "- /diff", "- /show <file-number>", "- /logs", "- /pr"]
    )
    return "\n".join(lines)
