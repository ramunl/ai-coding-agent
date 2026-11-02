"""Retain optional scope advice while preserving legacy bug triage parsing."""

from dataclasses import dataclass

from ai_agent.bugfix_questions import _extract_json_object, bugfix_questions
from ai_agent.plan_scope import assess_scope


@dataclass(frozen=True)
class BugfixReview:
    """Carry clarification and implementation scope from the same assessment."""

    questions: str | None
    files: list[str]
    steps: list[str]
    risks: list[str]
    scope_assessment: dict


def parse_bugfix_review(assessment: str, bug: str) -> BugfixReview:
    """Read new optional fields without changing legacy ready/questions semantics."""
    data = _extract_json_object(assessment) or {}

    def items(key: str) -> list[str]:
        values = data.get(key)
        if not isinstance(values, list):
            return []
        return [
            item.strip()[:500]
            for item in values[:8]
            if isinstance(item, str) and item.strip()
        ]

    files, steps, risks = items("files"), items("steps"), items("risks")
    # Use actual requested scope as a fallback, never fabricated file counts.
    requested_steps = [line.strip() for line in bug.splitlines() if line.strip()]
    return BugfixReview(
        bugfix_questions(assessment),
        files,
        steps,
        risks,
        assess_scope(
            data.get("scope_assessment"), files, steps or requested_steps, risks
        ),
    )
