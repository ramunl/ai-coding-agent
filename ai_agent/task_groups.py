"""Display titles for split task groups, including older saved task records."""

import re

from ai_agent.context_validation import _redact_text

TITLE_LIMIT = 300


def _title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_redact_text(value).split())[:TITLE_LIMIT]


def split_title(feature: str, source: dict | None = None) -> str:
    """Preserve the owner-visible parent title before replacing its task."""
    source = source or {}
    return _title(source.get("group_title") or source.get("title") or feature)


def group_title(task: dict) -> str | None:
    """Recover only a bounded display title; never interpret text as authority."""
    if not isinstance(task.get("group"), str) or not task["group"]:
        return None
    if title := _title(task.get("group_title")):
        return title
    text = task.get("text")
    if isinstance(text, str):
        for marker in (
            "Original request (context only): ",
            "Whole change (context only): ",
            "Whole change: ",
        ):
            if marker in text:
                line = text.split(marker, 1)[1].splitlines()
                if line and (title := _title(line[0])):
                    return title
    title = _title(task.get("title"))
    # Legacy repository splits repeated the original title on every child.
    match = re.fullmatch(r"(.+) \([^()]+, part [0-9]+/[0-9]+\)", title)
    return _title(match[1]) if match else None
