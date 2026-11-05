"""Account for explicitly requested repositories before plan approval."""

import re
from datetime import date

from ai_agent.config import redact_sensitive
from ai_agent.projects import list_projects

GUIDANCE = """
Account for every registered repository explicitly named in the original request.
Add repository_accounting entries with name, role (change, verify, completed,
context or unclear), reason, evidence (a source reference for completed work),
and checked_at (YYYY-MM-DD for completed work).
List only implementation owners in repositories, in dependency order; multiple
implementation owners require offering /split. Verification-only or context
repositories do not need implementation tasks. Give concrete verification steps
for verify entries. Completed claims need a dated evidence reference; never invent
checks or treat old evidence as fresh. Mark unresolved ownership unclear instead
of silently dropping work. Already split tasks account only for their Assigned
repository; parent requests and prerequisites are context, not new work.
"""


def accounting(raw: object) -> list[dict[str, str]]:
    """Preserve bounded, redacted provider statements, never execution authority."""
    if not isinstance(raw, list):
        return []
    return [
        {
            key: redact_sensitive(item[key].strip())[:500]
            if isinstance(item.get(key), str)
            else ""
            for key in ("name", "role", "reason", "evidence", "checked_at")
        }
        for item in raw[:20]
        if isinstance(item, dict)
    ]


def requested(feature: str, names: list[str] | None = None) -> list[str]:
    """Find whole registered names in owner text, respecting scoped children."""
    if names is None:
        names = [project.name for project in list_projects()]
    assigned = re.match(r"Assigned repository:\s*([A-Za-z0-9_.-]+)\.", feature)
    return (
        [assigned[1]]
        if assigned
        else [
            name
            for name in names
            if re.search(
                r"(?<![A-Za-z0-9_.-])"
                + re.escape(name)
                + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])",
                feature,
            )
        ]
    )


def issues(feature: str, document: object, names: list[str] | None = None) -> list[str]:
    """Missing or unresolved scope cannot be approved by either provider path."""
    if names is None:
        try:
            names = [project.name for project in list_projects()]
        except (OSError, RuntimeError, ValueError):
            return [
                "Cannot read registered repositories; restore the registry and revise."
            ]
    required = requested(feature, names)
    assigned = re.match(r"Assigned repository:\s*([A-Za-z0-9_.-]+)\.", feature)
    changes = {item["name"] for item in document.repositories if item.get("changes")}
    rows = document.repository_accounting
    required = list(
        dict.fromkeys(
            required
            + [
                row["name"]
                for row in rows
                if row["role"] == "change" and row["name"] in names
            ]
        )
    )
    result = []
    if assigned and changes - {assigned[1]}:
        result.append(
            "Already scoped task includes changes outside its assigned repository."
        )
    for name in required:
        matches = [row for row in rows if row["name"] == name]
        if not matches and name in changes:
            continue
        if len(matches) != 1:
            result.append(f"{name}: missing or duplicate repository accounting.")
            continue
        row = matches[0]
        role = row["role"]
        if role == "change" and name in changes:
            continue
        if (
            role not in {"verify", "completed", "context"}
            or not row["reason"]
            or name in changes
        ):
            result.append(f"{name}: unresolved or conflicting repository scope.")
            continue
        if role == "verify" and not any(name in step for step in document.steps):
            result.append(
                f"{name}: list a concrete verification step naming this repository."
            )
        if role == "completed":
            try:
                checked = date.fromisoformat(row["checked_at"])
                if checked.isoformat() != row["checked_at"] or checked > date.today():
                    raise ValueError
            except ValueError:
                result.append(f"{name}: completed work needs a valid evidence date.")
            if not row["evidence"]:
                result.append(f"{name}: completed work needs an evidence reference.")
    return result


def review(plan: object) -> list[str]:
    """Validate the original request, not the model's narrowed summary."""
    from ai_agent.plan_document import parse_plan_document

    return issues(plan.feature, parse_plan_document(plan.plan_text, plan.feature))
