"""Parse planner output into a plan document and its implementation prompt."""

import json
import re
from dataclasses import dataclass
from typing import Any

from ai_agent.workflow import slugify_branch_name


@dataclass(frozen=True)
class PlanDocument:
    """Represent parsed plan content and the implementation prompt."""

    branch: str
    summary: str
    files: list[str]
    steps: list[str]
    risks: list[str]
    codex_prompt: str


def parse_plan_document(plan_text: str, feature: str = "") -> PlanDocument:
    """Normalize structured or plain-text planner output into a plan."""
    data = _loads_plan_json(plan_text)
    if not data:
        fallback_branch = slugify_branch_name(
            feature or _first_line(plan_text) or "change"
        )
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


def _build_codex_prompt(
    summary: str, files: list[str], steps: list[str], risks: list[str]
) -> str:
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
        sections.extend(
            ["", "Files to create or modify:", *[f"- {file}" for file in files]]
        )
    has_steps = bool(steps)
    if has_steps:
        sections.extend(["", "Implementation steps:", *steps])
    has_risks = bool(risks)
    if has_risks:
        sections.extend(["", "Watch out for:", *[f"- {risk}" for risk in risks]])
    sections.extend(
        ["", "Follow the repository's existing conventions and coding rules."]
    )
    return "\n".join(sections)


def _loads_plan_json(plan_text: str) -> dict[str, Any] | None:
    """Parse Claude's plan JSON, tolerating markdown fences and truncation.

    Claude's output is not always clean JSON: it may be wrapped in a ```json
    fence, preceded by prose, or — most commonly — cut off before the closing
    brace when it exceeds the model's token budget. Each candidate below peels
    away one of those layers; the first that yields a JSON object wins so a
    partial plan still renders real fields instead of falling back to a
    plain-text dump of the raw JSON.
    """
    for candidate in _plan_json_candidates(plan_text.strip()):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _plan_json_candidates(text: str) -> list[str]:
    candidates = []
    unfenced = _strip_code_fence(text)
    candidates.append(unfenced)
    if unfenced != text:
        candidates.append(text)
    # Largest {...} span handles prose wrapped around well-formed JSON.
    span = re.search(r"\{.*\}", unfenced, flags=re.DOTALL)
    if span:
        candidates.append(span.group(0))
    # Repaired fragment recovers a plan truncated before the closing brace.
    repaired = _repair_truncated_json(unfenced)
    if repaired:
        candidates.append(repaired)
    return candidates


def _strip_code_fence(text: str) -> str:
    fenced = re.search(
        r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL
    )
    if fenced:
        return fenced.group(1).strip()
    # An opening fence with no closing fence means the output was truncated.
    opened = re.match(r"```(?:json)?\s*(.*)$", text, flags=re.IGNORECASE | re.DOTALL)
    if opened:
        return opened.group(1).strip()
    return text


def _repair_truncated_json(text: str) -> str | None:
    """Best-effort recovery of JSON that was cut off mid-object.

    Balances the fragment and, if it still won't parse, drops the last
    comma-separated member and retries — so complete fields survive even when
    the final one was truncated. Never discards a member that already parses.
    """
    start = text.find("{")
    if start == -1:
        return None
    fragment = text[start:]
    while fragment:
        repaired = _balance_json(fragment)
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            cut = fragment.rfind(",")
            if cut <= 0:
                return None
            fragment = fragment[:cut]
    return None


def _balance_json(fragment: str) -> str:
    """Close any open string and unclosed brackets so a fragment can parse."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in fragment:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if stack:
                stack.pop()
    repaired = fragment + ('"' if in_string else "")
    repaired = re.sub(r",\s*$", "", repaired.rstrip())
    for opener in reversed(stack):
        repaired += "]" if opener == "[" else "}"
    return repaired


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
