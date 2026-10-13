"""Parse bug-triage assessments and keep only product clarification questions."""

from __future__ import annotations

import json
import re

IMPLEMENTATION_QUESTION_PATTERNS = (
    "where is",
    "where are",
    "where in the code",
    "current implementation",
    "how is the current",
    "how is the new",
    "how is it calculated",
    "how is the new url",
    "how is the url",
    "what method",
    "what api",
    "which method",
    "which api",
    "what function",
    "which function",
    "stored/accessed",
    "stored or accessed",
    "located in the ui",
    "button located",
)


def _extract_json_object(text: str) -> dict | None:
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _looks_ready(text: str) -> bool:
    normalized = text.upper()
    has_ready = bool(re.search(r"\bREADY\b", normalized))
    has_questions_marker = bool(re.search(r"\bQUESTIONS\s*:", normalized))
    return has_ready and not has_questions_marker


def bugfix_questions(assessment: str) -> str | None:
    """Extract useful clarification questions from bug triage."""
    text = assessment.strip()
    assessment_json = _extract_json_object(text)
    if assessment_json:
        status = str(assessment_json.get("status", "")).strip().lower()
        if status == "ready":
            return None
        if status == "questions":
            questions_value = assessment_json.get("questions", [])
            if isinstance(questions_value, list):
                questions = "\n".join(
                    str(question).strip()
                    for question in questions_value
                    if str(question).strip()
                )
            else:
                questions = str(questions_value).strip()
            return _filter_product_questions(questions)

    if _looks_ready(text):
        return None

    if text.upper().startswith("QUESTIONS:"):
        questions = text.split(":", 1)[1].strip()
    else:
        questions = text

    return _filter_product_questions(questions)


def _filter_product_questions(questions: str) -> str | None:
    if not questions:
        return "Please provide the missing expected product behavior."

    question_lines = [line.strip() for line in questions.splitlines() if line.strip()]
    product_questions = []
    for line in question_lines:
        normalized = re.sub(r"^\d+[.)]\s*", "", line).strip().lower()
        if any(pattern in normalized for pattern in IMPLEMENTATION_QUESTION_PATTERNS):
            continue
        product_questions.append(line)

    if not product_questions:
        return None

    return "\n".join(product_questions)
