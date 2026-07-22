import json
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ai_agent.projects import active_project
from ai_agent.workflow import slugify_branch_name


class Verbosity(StrEnum):
    CONCISE = "concise"
    NORMAL = "normal"
    DEBUG = "debug"


@dataclass
class PlanState:
    id: str
    feature: str
    revision: int
    plan_text: str
    approved: bool
    history: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlanDocument:
    branch: str
    summary: str
    files: list[str]
    steps: list[str]
    risks: list[str]
    codex_prompt: str


@dataclass(frozen=True)
class ExecutionState:
    branch: str
    files_changed: list[str]
    diff_summary: str
    full_diff: str
    logs: str
    pr_url: str | None = None
    tests: str = "UNKNOWN"


def new_plan_state(feature: str, plan_text: str) -> PlanState:
    return PlanState(
        id=str(uuid.uuid4())[:8],
        feature=feature,
        revision=1,
        plan_text=plan_text,
        approved=False,
        history=[],
    )


def revise_plan_state(plan: PlanState, plan_text: str) -> PlanState:
    return PlanState(
        id=plan.id,
        feature=plan.feature,
        revision=plan.revision + 1,
        plan_text=plan_text,
        approved=False,
        history=[*plan.history, plan.plan_text],
    )


def parse_verbosity(value: str) -> Verbosity | None:
    normalized = value.strip().lower()
    for verbosity in Verbosity:
        if normalized == verbosity.value:
            return verbosity
    return None


def parse_plan_document(plan_text: str, feature: str = "") -> PlanDocument:
    data = _loads_plan_json(plan_text)
    if not data:
        fallback_branch = slugify_branch_name(feature or _first_line(plan_text) or "change")
        fallback_prompt = (
            "Implement the following change in this repository. Edit the files "
            "directly and make the actual code changes — do not merely describe "
            "them.\n\n" + plan_text
        )
        return PlanDocument(
            branch=fallback_branch,
            summary=_first_line(plan_text) or feature or "Planned change",
            files=[],
            steps=_plain_text_steps(plan_text),
            risks=[],
            codex_prompt=fallback_prompt,
        )

    summary = _string_value(data, "summary") or feature or "Planned change"
    branch = _normalize_branch(_string_value(data, "branch"), summary)
    files = _string_list(data.get("files"))
    steps = _string_list(data.get("steps"))
    risks = _string_list(data.get("risks"))
    # Claude's plan has no codex_prompt field; build an executable instruction
    # from the structured plan rather than handing Codex the raw plan document.
    explicit_prompt = _string_value(data, "codex_prompt")
    codex_prompt = explicit_prompt or _build_codex_prompt(summary, files, steps, risks)
    return PlanDocument(
        branch=branch,
        summary=summary,
        files=files,
        steps=steps,
        risks=risks,
        codex_prompt=codex_prompt,
    )


def _build_codex_prompt(summary: str, files: list[str], steps: list[str], risks: list[str]) -> str:
    """Turn a parsed plan into a direct implementation instruction for the agent."""
    sections = [
        "Implement the following change in this repository. "
        "Edit the files directly and make the actual code changes — "
        "do not merely describe or plan them.",
        "",
        f"Change: {summary}",
    ]
    has_files = bool(files)
    if has_files:
        sections.extend(["", "Files to create or modify:", *[f"- {file}" for file in files]])
    has_steps = bool(steps)
    if has_steps:
        sections.extend(["", "Implementation steps:", *steps])
    has_risks = bool(risks)
    if has_risks:
        sections.extend(["", "Watch out for:", *[f"- {risk}" for risk in risks]])
    sections.extend(["", "Follow the repository's existing conventions and coding rules."])
    return "\n".join(sections)


def render_plan(plan: PlanState) -> str:
    document = parse_plan_document(plan.plan_text, plan.feature)
    project = active_project()
    lines = [
        f"Plan #{plan.id}",
        f"Revision: {plan.revision}",
        "",
        "Project:",
        f"{project.name} ({project.github_repository})",
        "",
        "Branch:",
        document.branch,
        "",
        "Summary:",
        document.summary,
    ]
    if document.files:
        lines.extend(["", "Files:", *[f"- {file}" for file in document.files]])
    if document.steps:
        lines.extend(["", "Implementation:", *[f"{index}. {step}" for index, step in enumerate(document.steps, 1)]])
    if document.risks:
        lines.extend(["", "Risks:", *[f"- {risk}" for risk in document.risks]])
    lines.extend(["", "Commands:", "- /discuss <feedback>", "- /approve", "- /cancel"])
    return "\n".join(lines)


def render_history(plan: PlanState) -> str:
    revisions = [*plan.history, plan.plan_text]
    lines = [f"Plan #{plan.id} history"]
    for index, text in enumerate(revisions, 1):
        document = parse_plan_document(text, plan.feature)
        lines.append(f"Revision {index}: {document.summary}")
    return "\n".join(lines)


def render_diff_summary(diff_text: str, files: list[str]) -> str:
    added = sum(1 for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_text.splitlines() if line.startswith("-") and not line.startswith("---"))
    lines = ["Modified files:"]
    if files:
        lines.extend(f"{index}. {file}" for index, file in enumerate(files, 1))
    else:
        lines.append("(none)")
    lines.extend(["", f"+{added} lines", f"-{removed} lines"])
    return "\n".join(lines)


def render_completion(execution: ExecutionState, verbosity: Verbosity) -> str:
    heading = "Implementation failed." if execution.tests == "FAIL" else "Implementation completed."
    lines = [heading, "", f"Files changed: {len(execution.files_changed)}", f"Tests: {execution.tests}"]
    if execution.pr_url:
        lines.append(f"PR: {execution.pr_url}")
    if verbosity in {Verbosity.NORMAL, Verbosity.DEBUG} and execution.files_changed:
        lines.extend(["", "Files changed:", *[f"- {file}" for file in execution.files_changed]])
    if verbosity == Verbosity.DEBUG:
        if execution.full_diff:
            lines.extend(["", "Diff:", execution.full_diff])
        if execution.logs:
            lines.extend(["", "Logs:", execution.logs])
    lines.extend(["", "Commands:", "- /diff", "- /show <file-number>", "- /logs", "- /pr"])
    return "\n".join(lines)


def _loads_plan_json(plan_text: str) -> dict[str, Any] | None:
    text = plan_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _normalize_branch(branch: str, fallback: str) -> str:
    if "/" not in branch:
        return slugify_branch_name(fallback)
    prefix, slug = branch.split("/", 1)
    try:
        return slugify_branch_name(slug or fallback, prefix or "feature")
    except ValueError:
        return slugify_branch_name(fallback)


def _string_value(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _plain_text_steps(text: str) -> list[str]:
    return [line.strip(" -") for line in text.splitlines() if line.strip()][:8]
