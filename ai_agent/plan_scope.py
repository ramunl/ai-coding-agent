"""Assess implementation scope without predicting provider session capacity."""

from __future__ import annotations

from ai_agent.config import redact_sensitive

SCOPE_GUIDANCE = """
Assess implementation scope before approval, including work within ONE repository.
Include scope_assessment: {"recommend_split": boolean, "reason": "specific reason",
"milestones": [{"title": "bounded change", "acceptance": "independent test"}]}.
Recommend splitting when there are several independently testable changes,
substantial integration/migration work, or uncertain prerequisites. Supply 2-5
ordered milestones with concrete acceptance checks; otherwise use false and [].
Do not estimate hours or promise completion within any session. Already scoped
milestones should not expand their parent initiative or repeat prerequisites.
This is advice only: do not execute or create tasks automatically.
"""


def _text(value: object, limit: int) -> str:
    return redact_sensitive(value.strip())[:limit] if isinstance(value, str) else ""


def assess_scope(
    raw: object, files: list[str], steps: list[str], risks: list[str] | None = None
) -> dict:
    """Normalize bounded provider advice, conservatively assess legacy plans."""
    fallback = _fallback(files, steps, risks or [])
    if not isinstance(raw, dict) or not isinstance(raw.get("recommend_split"), bool):
        return fallback
    milestones = []
    values = raw.get("milestones")
    if isinstance(values, list):
        for item in values[:5]:
            if not isinstance(item, dict):
                continue
            title = _text(item.get("title"), 200)
            acceptance = _text(item.get("acceptance"), 500)
            if title and acceptance:
                milestones.append({"title": title, "acceptance": acceptance})
    if raw["recommend_split"]:
        return {
            "recommend_split": True,
            "reason": _text(raw.get("reason"), 500)
            or fallback["reason"]
            or "The planner recommends smaller independently testable milestones.",
            "milestones": milestones or fallback["milestones"],
        }
    # A provider can omit scope or understate an obviously broad legacy plan.
    return fallback


def _fallback(files: list[str], steps: list[str], risks: list[str]) -> dict:
    text = " ".join([*steps, *risks]).lower()
    concerns = (
        ("persist", "storage", "migration"),
        ("retry", "resume", "recovery", "restart"),
        ("cancel", "interrupt", "lifecycle"),
        ("dashboard", "telegram", "api"),
    )
    concern_count = sum(any(word in text for word in group) for group in concerns)
    broad = (
        len(files) >= 8 or len(steps) >= 7 or (len(steps) >= 4 and concern_count >= 3)
    )
    if not broad:
        return {"recommend_split": False, "reason": "", "milestones": []}
    # Group actual steps, never invent implementation details from filenames.
    work_items = steps or [f"Implement and verify {path}" for path in files]
    groups = [work_items[index : index + 2] for index in range(0, len(work_items), 2)][
        :5
    ]
    milestones = [
        {
            "title": _text("; ".join(group), 200),
            "acceptance": "Verify this milestone with focused tests and the required "
            "build before starting the next milestone.",
        }
        for group in groups
    ]
    return {
        "recommend_split": True,
        "reason": "This plan spans many files, steps, or interacting lifecycle "
        "concerns. Consider "
        "smaller independently testable milestones before approving; "
        "this is a scope heuristic, not a session-duration estimate.",
        "milestones": milestones,
    }


def scope_warning(assessment: dict) -> list[str]:
    """Render actionable scope advice before approval in Telegram."""
    if not assessment.get("recommend_split"):
        return []
    lines = [
        "",
        "Scope warning: splitting this task is recommended.",
        assessment["reason"],
    ]
    for index, item in enumerate(assessment["milestones"], 1):
        lines.append(f"{index}. {item['title']} — Check: {item['acceptance']}")
    lines.append(
        "Use /discuss to revise this plan to one milestone, or approve "
        "the full scope if you prefer. No tasks are created automatically."
    )
    return lines
