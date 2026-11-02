"""Task-bound Git checkpoints; refs are generated internally, never user input."""

import hashlib
import json
import re
from contextvars import ContextVar
from pathlib import Path

from ai_agent.projects import active_project
from ai_agent.shell import run

_repository = ContextVar("checkpoint_repository", default=None)


def _repo() -> Path:
    return _repository.get() or active_project().repo_path


def _git(*args: str) -> str:
    return run(["git", *args], cwd=_repo()).output.strip()


def identity() -> dict:
    """Identify both checkout and origin remote."""
    return {
        "path": str(_repo().resolve()),
        "remote": _git("remote", "get-url", "origin"),
        "git_dir": str((_repo() / _git("rev-parse", "--git-common-dir")).resolve()),
    }


def _path(task_id: str) -> Path:
    # Hash even internal identifiers: no caller-controlled paths.
    name = hashlib.sha256(str(task_id).encode()).hexdigest()
    return Path(identity()["git_dir"]) / "ai-agent-checkpoints" / (name + ".json")


def save(record: dict) -> None:
    """Atomically replace the private task manifest."""
    path = _path(record["task"])
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(path)


def load(task_id: str, branch: str) -> dict:
    """Validate manifest scope and generated reference syntax."""
    record = json.loads(_path(task_id).read_text())
    if not isinstance(record, dict):
        raise ValueError("Checkpoint is not an object.")
    if record.get("state") not in ("running", "saved", "complete"):
        raise ValueError("Checkpoint state is invalid.")
    if (
        record.get("version") != 1
        or record.get("identity") != identity()
        or record.get("task") != str(task_id)
        or record.get("branch") != branch
    ):
        raise ValueError("Checkpoint task, repository or branch does not match.")
    for key in ("base", "head"):
        if not re.fullmatch(r"[0-9a-f]{40,64}", str(record.get(key, ""))):
            raise ValueError("Checkpoint has an invalid commit.")
    refs = record.get("refs", [])
    if not isinstance(refs, list) or any(
        not isinstance(ref, str)
        or not re.fullmatch(r"refs/ai-agent-recovery/[0-9a-f]{32}/(commits|stash)", ref)
        for ref in refs
    ):
        raise ValueError("Checkpoint has an invalid recovery reference.")
    objects = record.get("objects", {})
    if not isinstance(objects, dict) or any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40,64}", value)
        for value in objects.values()
    ):
        raise ValueError("Checkpoint object identities are invalid.")
    return record


def begin(task_id: str, branch: str) -> None:
    """Write before the provider runs so a service crash retains ownership."""
    save(
        {
            "version": 1,
            "task": str(task_id),
            "identity": identity(),
            "branch": branch,
            "base": _git("rev-parse", "HEAD"),
            "head": _git("rev-parse", "HEAD"),
            "state": "running",
            "refs": [],
        }
    )


def capture(task_id: str, branch: str, references: list[str]) -> None:
    """Record immutable recovery refs after the provider exits."""
    record = load(task_id, branch)
    if _git("branch", "--show-current") != branch:
        raise ValueError("Checkpoint checkout is on another branch.")
    record.update(
        head=_git("rev-parse", "HEAD"),
        refs=list(references),
        state="saved",
        objects={ref: _git("rev-parse", "--verify", ref) for ref in references},
    )
    save(record)


def status(task_id: str, branch: str) -> dict:
    """Describe whether verified nonempty progress can be restored."""
    try:
        record = load(task_id, branch)
        if record.get("pr_url"):
            return {
                "can_resume": False,
                "resume_reason": (
                    "PR exists; use PR repair. Saved CI edits need manual recovery."
                ),
                "resume_summary": "",
            }
        if record["state"] == "complete":
            raise ValueError("The previous attempt already completed.")
        if record["state"] == "running":
            if _git("branch", "--show-current") != branch:
                raise ValueError(
                    "Interrupted checkout moved; inspect recovery manually."
                )
            _git("merge-base", "--is-ancestor", record["base"], "HEAD")
            progress = _git("rev-parse", "HEAD") != record["base"] or bool(
                _git("status", "--porcelain", "--untracked-files=all")
            )
        else:
            if _git("rev-parse", "--verify", "refs/heads/" + branch) != record["head"]:
                raise ValueError("Checkpoint branch has changed.")
            _git("merge-base", "--is-ancestor", record["base"], record["head"])
            progress = record["head"] != record["base"] or any(
                ref.endswith("/stash") for ref in record["refs"]
            )
            for ref in record["refs"]:
                resolved = _git("rev-parse", "--verify", ref)
                if resolved != record.get("objects", {}).get(ref):
                    raise ValueError("Checkpoint recovery reference changed.")
                if ref.endswith("/commits") and resolved != record["head"]:
                    raise ValueError("Checkpoint recovery commit changed.")
        if not progress:
            raise ValueError("No saved implementation progress; start fresh.")
        return {
            "can_resume": True,
            "resume_reason": "",
            "resume_summary": (
                f"Saved branch {branch}, commit {record['head'][:12]}; "
                "restore tracked and untracked edits."
            ),
        }
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return {
            "can_resume": False,
            "resume_reason": (
                "No verified task checkpoint; start fresh or inspect recovery manually."
            ),
            "resume_summary": "",
        }


def restore(task_id: str, branch: str) -> dict:
    """Caller holds recovery lease and has verified no live provider."""
    record = load(task_id, branch)
    if not status(task_id, branch)["can_resume"]:
        raise RuntimeError("No safe nonempty checkpoint is available.")
    if record["state"] == "running":
        # Still in the original checkout after restart: preserve before moving.
        from ai_agent.git_recovery import current_references, preserve_changes

        preserve_changes()
        capture(task_id, branch, current_references())
        record = load(task_id, branch)
    from ai_agent.git_recovery import require_clean_checkout

    require_clean_checkout()
    if _git("rev-parse", "--verify", f"refs/heads/{branch}") != record["head"]:
        raise RuntimeError("Saved branch changed since checkpoint; resume refused.")
    _git("checkout", "--no-overwrite-ignore", branch)
    for ref in record["refs"]:
        if ref.endswith("/stash"):
            _git("stash", "apply", "--index", ref)
    record["state"] = "running"
    save(record)
    return record


def complete(task_id: str, branch: str) -> None:
    """Invalidate resume once publication succeeded."""
    record = load(task_id, branch)
    record["state"] = "complete"
    save(record)


def task_status(task: dict) -> dict:
    """Read a registered task's checkpoint without switching active projects."""
    unavailable = {
        "can_resume": False,
        "resume_reason": "No saved attempt.",
        "resume_summary": "",
    }
    run_data = task.get("run") or {}
    original = run_data.get("repository_identity") or {}
    path = original.get("repo_path")
    if not path or not task.get("branch"):
        return unavailable
    token = _repository.set(Path(path))
    try:
        result = status(task["id"], task["branch"])
        # Do not advertise a live run as resumable, even with saved progress.
        from ai_agent.provider_process import require_no_writer

        require_no_writer(_repo())
        return result
    except (OSError, RuntimeError, ValueError):
        return {**unavailable, "resume_reason": "Provider shutdown is not verified."}
    finally:
        _repository.reset(token)


def published(task_id: str, branch: str, pr_url: str) -> None:
    """Keep checkpoint provenance while preventing duplicate PR publication."""
    record = load(task_id, branch)
    record["pr_url"] = pr_url
    save(record)


def save_interrupted_checkout() -> None:
    """Associate restart leftovers before any different task moves the checkout."""
    directory = Path(identity()["git_dir"]) / "ai-agent-checkpoints"
    if not directory.exists():
        return
    branch = _git("branch", "--show-current")
    matches = []
    for path in directory.glob("*.json"):
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise RuntimeError("Corrupt checkpoint; inspect before moving checkout.")
        if value.get("state") == "running" and value.get("branch") == branch:
            record = load(value.get("task", ""), branch)
            _git("merge-base", "--is-ancestor", record["base"], "HEAD")
            matches.append(record)
    if len(matches) > 1:
        raise RuntimeError(
            "Ambiguous interrupted checkout; inspect before starting fresh."
        )
    if matches:
        from ai_agent.git_recovery import current_references, preserve_changes

        before = current_references()
        preserve_changes()
        capture(
            matches[0]["task"],
            branch,
            [ref for ref in current_references() if ref not in before],
        )
