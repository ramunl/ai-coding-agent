"""Turn recurring errors in the agents' logs into proposed tasks.

Once a day, when the worker has nothing to do, it reads the last 24 hours of
error-level journal lines of each agent service, groups lines that differ only
in numbers, ids and times, and proposes a task for the most frequent groups.
No AI is used here: the planner looks at the error when the owner accepts the
proposal and the task is planned.

A proposal is a task in the "proposed" stage: the worker never starts it; the
owner accepts it (it becomes To do) or dismisses it. Each error group has a
signature; one that was proposed or dismissed is not proposed again for
QUIET_DAYS, so the same noise does not come back every day.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field

from ai_agent.config import redact_sensitive
from ai_agent.tasks import add_task, set_stage, tasks_of

# Service -> the project (repository) whose code it runs.
SERVICES = {
    "ai-coding-agent": "ai-coding-agent",
    "ai-pm-agent": "ai-pm-agent",
    "ai-ops-agent": "ai-ops-agent",
    "ai-dashboard": "ai-dashboard",
}
SCAN_EVERY = 24 * 3600
QUIET_DAYS = 30
MAX_PROPOSALS = 3
EVIDENCE_LINES = 3
LINE_LENGTH = 300

Reader = Callable[[str], list[str]]

_PREFIX = re.compile(r"^\S+\s+\S+\s+\S+?(\[\d+\])?:\s*")  # time host unit[pid]:
_VOLATILE = (
    (re.compile(r"\b[0-9a-f]{7,64}\b", re.IGNORECASE), "<id>"),
    (
        re.compile(
            r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?"
        ),
        "<time>",
    ),
    (re.compile(r"\d+(\.\d+)?"), "<n>"),
)


@dataclass
class ErrorGroup:
    """Error lines of one service that say the same thing."""

    service: str
    signature: str
    pattern: str
    count: int = 0
    samples: list[str] = field(default_factory=list)


def read_journal(service: str) -> list[str]:
    """Error-level lines of one service from the last 24 hours."""
    done = subprocess.run(
        [
            "journalctl",
            "-u",
            service,
            "-p",
            "err",
            "--since",
            "24 hours ago",
            "-o",
            "short-iso",
            "--no-pager",
            "-n",
            "10000",
            "-q",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done.stdout.splitlines() if done.returncode == 0 else []


def normalize(line: str) -> str:
    """The message without its prefix and the parts that change between lines."""
    message = _PREFIX.sub("", line.strip(), count=1)
    for pattern, placeholder in _VOLATILE:
        message = pattern.sub(placeholder, message)
    return " ".join(message.split())[:LINE_LENGTH]


def group_errors(service: str, lines: list[str]) -> list[ErrorGroup]:
    """Lines grouped by what they say, most frequent first."""
    groups: dict[str, ErrorGroup] = {}
    for line in lines:
        safe = redact_sensitive(line.strip())
        safe = re.sub(
            r"(?:sk-[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]+|\d{6,}:[A-Za-z0-9_-]{20,})",
            "[redacted]",
            safe,
        )
        safe = re.sub(
            r"(?i)(authorization: bearer |(?:token|password|secret|api_key)=)\S+",
            r"\1[redacted]",
            safe,
        )
        pattern = normalize(safe)
        if not pattern or pattern.startswith("-- "):
            continue
        signature = hashlib.sha1(f"{service}|{pattern}".encode()).hexdigest()[:16]
        group = groups.setdefault(signature, ErrorGroup(service, signature, pattern))
        group.count += 1
        if len(group.samples) < EVIDENCE_LINES:
            group.samples.append(pattern)
    return sorted(groups.values(), key=lambda group: group.count, reverse=True)


def quiet(data: MutableMapping) -> dict[str, float]:
    """Signature -> when it was last proposed or dismissed (saved)."""
    scan = data.setdefault("log_scan", {})
    seen = scan.get("seen")
    if not isinstance(seen, dict):
        seen = {}
        scan["seen"] = seen
    return seen


def is_quiet(data: MutableMapping, signature: str, now: float) -> bool:
    """Proposed or dismissed recently, or still an open task."""
    if now - quiet(data).get(signature, 0) < QUIET_DAYS * 24 * 3600:
        return True
    return any(
        task.get("signature") == signature
        and task.get("stage") not in ("done", "stopped")
        for task in tasks_of(data)
    )


def is_due(data: MutableMapping, now: float) -> bool:
    """A day has passed since the last scan."""
    return now - float(data.get("log_scan", {}).get("last", 0)) >= SCAN_EVERY


def scan(
    data: MutableMapping,
    projects: set[str],
    now: float,
    read: Reader = read_journal,
) -> list[dict]:
    """Propose tasks for the most frequent new errors; return the new tasks."""
    data.setdefault("log_scan", {})["last"] = now
    candidates = []
    for service, repo in SERVICES.items():
        if repo not in projects:
            continue  # no project to fix it in
        for group in group_errors(service, read(service)):
            if group.count >= 2 and not is_quiet(data, group.signature, now):
                candidates.append((group, repo))
    candidates.sort(key=lambda item: item[0].count, reverse=True)
    proposed = []
    for group, repo in candidates[:MAX_PROPOSALS]:
        title = f"Fix error in {group.service}: {group.pattern}"
        task = add_task(data, title, repo, None)
        evidence = "\n".join(group.samples)
        task["text"] = (
            f"{title}\n\nSeen {group.count} time(s) in the last 24 hours in the "
            f"{group.service} journal. Find the cause and fix it; add a test that "
            f"reproduces it.\n\nExamples:\n{evidence}"
        )[:4000]
        task.update(signature=group.signature, source="logs", evidence=group.samples)
        task["count"] = group.count
        set_stage(task, "proposed", f"seen {group.count}× in 24 h in {group.service}")
        quiet(data)[group.signature] = now
        proposed.append(task)
    return proposed


def dismiss(data: MutableMapping, task: dict, now: float) -> None:
    """Remember a dismissed proposal so its error is not proposed again soon."""
    if task.get("signature"):
        quiet(data)[task["signature"]] = now
