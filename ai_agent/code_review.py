"""Code reviews by the worker: one repository a day, a few Proposed tasks.

When the worker is idle it reviews the next registered repository in turn
(one every REVIEW_EVERY), read-only, with the repository's brief and the
coding rules, and asks for at most N proposals (a setting; 0 switches reviews
off), each a bug, an improvement or a feature with the files involved. They
arrive as "proposed" tasks you accept or dismiss, like those from the logs; a
dismissed or already proposed one is not proposed again for 30 days.

The review runs the planner tool in the repository itself: Codex with a
read-only sandbox, or Claude Code in plan mode, so neither can change files.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Callable, MutableMapping
from pathlib import Path

from ai_agent import log_scan
from ai_agent.codex_models import model_args
from ai_agent.config import redact_sensitive
from ai_agent.planning_provider import schema_path
from ai_agent.project_brief import project_brief
from ai_agent.rules import rules_prompt_block
from ai_agent.shell import run
from ai_agent.tasks import add_task, set_stage

REVIEW_EVERY = 24 * 3600
DEFAULT_PROPOSALS = 3
MAX_PROPOSALS = 10
TIMEOUT_SECONDS = 1200
KINDS = ("bug", "improvement", "feature")
TITLE_LIMIT = 150
DETAIL_LIMIT = 1500

# (tool, prompt, repo path) -> the tool's final text.
Reviewer = Callable[[str, str, Path], str]


def state(data: MutableMapping) -> dict:
    """{"last": when, "next": index of the next repository, "done": last result}."""
    review = data.get("worker", {}).setdefault("review", {})
    review.setdefault("last", 0)
    review.setdefault("next", 0)
    return review


def is_due(data: MutableMapping, now: float) -> bool:
    """True when the last review was a day ago or never happened."""
    return now - float(state(data).get("last", 0)) >= REVIEW_EVERY


def next_repository(data: MutableMapping, names: list[str]) -> str | None:
    """The repository to review now, in turn; None when none is registered."""
    if not names:
        return None
    return sorted(names)[int(state(data)["next"]) % len(names)]


def prompt(repo: str, brief: str, rules: str, count: int, known: list[str]) -> str:
    """What the reviewer is asked."""
    avoid = "\n".join(f"- {title}" for title in known[:30]) or "- (none)"
    return f"""You are reviewing the {repo} repository; you are in its root. Read
the code; do not change anything.

Project brief:
{brief or "(no AGENTS.md or README)"}
{rules}
Propose at most {count} changes that matter most, each one of:
- bug: something that is wrong now (say how it fails);
- improvement: reliability, security, performance or maintainability;
- feature: a small, clearly useful addition that fits the project.
Each must be concrete and checkable: name the files and what to change. Skip
style nits, renames and anything already in this list:
{avoid}

Return only JSON: {{"proposals": [{{"kind": "bug", "title": "...", "detail":
"...", "files": ["path"]}}]}}. Fewer good proposals beat filler; [] is fine.
"""


def run_review(tool: str, text: str, repo_path: Path) -> str:
    """Run the reviewer in the repository, read-only; its final text."""
    if tool == "claude":
        result = run(
            ["claude", "-p", text, "--permission-mode", "plan"],
            cwd=repo_path,
            timeout=TIMEOUT_SECONDS,
        )
        return result.output
    with tempfile.NamedTemporaryFile(prefix="ai-review-", suffix=".json") as output:
        run(
            [
                "codex",
                "exec",
                *model_args(),
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--output-schema",
                str(schema_path("code_review.json")),
                "--output-last-message",
                output.name,
                text,
            ],
            cwd=repo_path,
            timeout=TIMEOUT_SECONDS,
        )
        return Path(output.name).read_text(encoding="utf-8")


def parse(text: str, count: int) -> list[dict]:
    """The proposals in the reviewer's answer, cleaned and bounded."""
    data = None
    for candidate in [text, *re.findall(r"\{.*\}", text, re.DOTALL)]:
        try:
            data = json.loads(candidate)
            break
        except ValueError:
            continue
    items = data.get("proposals") if isinstance(data, dict) else None
    proposals = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        title = " ".join(str(item.get("title") or "").split())[:TITLE_LIMIT]
        if not title:
            continue
        files = [str(name)[:200] for name in item.get("files") or []][:5]
        detail = redact_sensitive(str(item.get("detail") or ""))[:DETAIL_LIMIT]
        proposals.append(
            {"kind": item["kind"], "title": title, "detail": detail, "files": files}
        )
    return proposals[:count]


def signature(repo: str, title: str) -> str:
    """Stable id of a proposal, so it is not proposed twice."""
    key = f"review:{repo}:{' '.join(title.lower().split())}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def review(
    data: MutableMapping,
    repo: str,
    repo_path: Path,
    tool: str,
    count: int,
    now: float,
    reviewer: Reviewer = run_review,
) -> list[dict]:
    """Review one repository; add its new proposals as Proposed tasks."""
    review_state = state(data)
    review_state["last"] = now
    review_state["next"] = int(review_state["next"]) + 1
    brief = project_brief(repo_path)[1]
    known = [
        task["title"]
        for task in data.get("tasks") or []
        if task.get("repo") == repo and task.get("source") == "review"
    ]
    answer = reviewer(
        tool, prompt(repo, brief, rules_prompt_block(), count, known), repo_path
    )
    created = []
    for proposal in parse(answer, count):
        key = signature(repo, proposal["title"])
        if log_scan.is_quiet(data, key, now):
            continue
        log_scan.quiet(data)[key] = now
        text = f"{proposal['title']}\n\n{proposal['detail']}"
        if proposal["files"]:
            text += "\n\nFiles: " + ", ".join(proposal["files"])
        task = add_task(data, proposal["title"], repo, None)
        task["text"] = " ".join(text.split())[:4000]
        task.update(
            source="review",
            signature=key,
            evidence=[proposal["detail"][:300], *proposal["files"]],
        )
        set_stage(task, "proposed", f"{proposal['kind']} · code review")
        created.append(task)
    review_state["done"] = {"repo": repo, "at": now, "proposed": len(created)}
    return created
