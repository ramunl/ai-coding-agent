"""Todos tagged #auto become Autopilot tasks, so the worker always has work.

The PM agent owns the todo lists; this module only reads them, through the
PM agent's local JSON bridge (the same one the dashboard uses), and never
writes to them. An open todo whose text has the tag ``#auto`` becomes one
Autopilot task, once: the todo's reference is remembered, so a task you
remove is not made again. ``#<repository>`` (a registered repository name)
picks where it is planned; otherwise a repository named like the todo list,
else the active project. The plan can still move or split it (task_split).
When the task is deployed the todo is closed, as for any task from a todo.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Iterable, MutableMapping

from ai_agent.tasks import add_task, tasks_of

logger = logging.getLogger(__name__)

PM_COMMAND = "/usr/local/sbin/ai-pm-todos"
SCAN_EVERY = 10 * 60
MAX_REMEMBERED = 500
MAX_NEW_PER_SCAN = 3
TAG = re.compile(r"(?<![\w#])#auto\b", re.IGNORECASE)
_HASHTAG = re.compile(r"(?<![\w#])#([A-Za-z0-9._-]+)")
_ID = re.compile(r"^[a-f0-9]{32}$")
_PROJECT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# JSON request -> JSON response, or None when the bridge can't be reached.
Bridge = Callable[[dict], "dict | None"]


def ask_pm(request: dict) -> dict | None:
    """One request to the PM agent's todo bridge; None if it fails."""
    try:
        result = subprocess.run(
            [PM_COMMAND],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        answer = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        logger.info("PM todo bridge unavailable: %s", error)
        return None
    return answer if isinstance(answer, dict) and answer.get("ok") else None


def tagged_todos(bridge: Bridge = ask_pm) -> list[dict]:
    """Open #auto todos of every PM list: [{"project", "id", "text"}]."""
    first = bridge({"action": "read"})
    if first is None:
        return []
    projects = [
        item.get("name")
        for item in first.get("workspace", {}).get("projects", [])
        if isinstance(item, dict) and _PROJECT.match(str(item.get("name")))
    ]
    found = []
    for project in projects:
        answer = bridge({"action": "read", "project": project})
        for item in (answer or {}).get("workspace", {}).get("items", []):
            text = str(item.get("text") or "")
            item_id = str(item.get("id") or "")
            if item.get("status") == "done" or not TAG.search(text):
                continue
            if _ID.match(item_id):
                found.append({"project": project, "id": item_id, "text": text})
    return found


def repository_for(todo: dict, registered: Iterable[str], active: str) -> str:
    """Where to plan it: a #<repository> tag, the list's name, else active."""
    names = set(registered)
    for tag in _HASHTAG.findall(todo["text"]):
        if tag in names:
            return tag
    return todo["project"] if todo["project"] in names else active


def task_text(text: str, registered: Iterable[str]) -> str:
    """The todo text without #auto and #<repository> tags."""
    names = set(registered)
    text = TAG.sub("", text)
    text = _HASHTAG.sub(lambda match: "" if match[1] in names else match[0], text)
    return " ".join(text.split())


def is_due(data: MutableMapping, now: float) -> bool:
    """True when the last scan was SCAN_EVERY ago or never happened."""
    return now - float(data.get("auto_todos", {}).get("last", 0)) >= SCAN_EVERY


def make_tasks(
    data: MutableMapping,
    todos: list[dict],
    registered: Iterable[str],
    active: str,
    now: float,
) -> list[dict]:
    """Add an Autopilot task for each new #auto todo; return the new tasks."""
    registered = list(registered)
    state = data.setdefault("auto_todos", {})
    seen = state.setdefault("seen", [])
    state["last"] = now
    known = set(seen) | {task.get("todo") for task in tasks_of(data)}
    created = []
    for todo in todos:
        ref = f"{todo['project']}:{todo['id']}"
        if ref in known or len(created) == MAX_NEW_PER_SCAN:
            continue
        text = task_text(todo["text"], registered)
        if not text:
            continue
        task = add_task(data, text, repository_for(todo, registered, active), ref)
        task["autopilot"] = True
        task["source"] = "todo #auto"
        seen.append(ref)
        known.add(ref)
        created.append(task)
    del seen[:-MAX_REMEMBERED]
    return created
