"""Represent no-code runs honestly, without inventing a PR or completion."""

from __future__ import annotations

import json
import time
from collections.abc import MutableMapping

from ai_agent.config import redact_sensitive


class NoCodeOutcomeError(RuntimeError):
    """An implementation ended with a report rather than repository changes."""

    def __init__(self, output: str) -> None:
        """Keep a bounded, redacted explanation and a conservative outcome."""
        text = redact_sensitive(output).strip()
        self.status = "blocked"
        try:
            data = json.loads(text.removeprefix("```json").removesuffix("```").strip())
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("outcome") in (
            "blocked",
            "ops_required",
        ):
            self.status = data["outcome"]
            text = "\n".join(
                f"{key.replace('_', ' ').title()}: {data[key]}"
                for key in ("summary", "evidence", "next_step")
                if isinstance(data.get(key), str)
            )
        self.report = (
            "\n".join(text.splitlines()[:18])[:3500]
            or "The agent returned no explanation."
        )
        title = "Needs Ops action" if self.status == "ops_required" else "Blocked"
        super().__init__(f"{title}: no commit or PR was created.\n{self.report}")


def record_outcome(
    data: MutableMapping, branch: str, outcome: NoCodeOutcomeError
) -> None:
    """Persist a report and move linked tasks without marking their todos done."""
    data["last_run_outcome"] = {
        "branch": branch,
        "status": outcome.status,
        "report": outcome.report,
        "at": time.time(),
    }
    for task in data.get("tasks") or []:
        if task.get("branch") == branch:
            task.update(stage=outcome.status, note=outcome.report)


def discard_empty_branch(branch: str) -> None:
    """Remove a failed run's branch only if it exactly matches the base commit."""
    from ai_agent.projects import active_project
    from ai_agent.shell import run
    from ai_agent.workflow import validate_branch_name

    validate_branch_name(branch)
    base = active_project().base_branch
    if branch == base:
        return
    branch_sha = run(["git", "rev-parse", "--verify", branch]).output.strip()
    base_sha = run(["git", "rev-parse", "--verify", base]).output.strip()
    if branch_sha == base_sha:
        run(["git", "branch", "-d", branch])
