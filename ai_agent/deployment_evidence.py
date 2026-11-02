"""Read the existing Ops manager journal; diagnostics and prompts are not authority."""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

from ai_agent.context_models import RepositoryIdentity

DEPLOYMENT_AGE = 900
# Fixed authoritative Ops path. No caller-selected file or dashboard fallback.
STATE_PATH = "/var/lib/ai-deploy/state.json"
TARGETS = frozenset({"ai-coding-agent", "ai-pm-agent", "ai-ops-agent", "ai-dashboard"})
_READER = """
import os, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, 'rb') as stream:
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
        raise ValueError('invalid deployment journal')
    value = stream.read(262145)
    if len(value) > 262144:
        raise ValueError('deployment journal too large')
    sys.stdout.buffer.write(value)
"""


def read_state() -> dict:
    """Bound read duration and bytes without creating/chmodding Ops state."""
    result = subprocess.run(
        [sys.executable, "-c", _READER, STATE_PATH],
        capture_output=True,
        timeout=3,
        check=False,
    )
    if result.returncode:
        raise ValueError("Authoritative deployment evidence unavailable")
    return json.loads(result.stdout)


def validate_deployment(state: dict, target: str, now: float) -> dict:
    """verified_at is a successful restart/probe timestamp, not collection time."""
    if target not in TARGETS or state["active"] is not None:
        raise ValueError("Deployment target unavailable or transaction active")
    record = state["targets"][target]
    history = record["history"][-1]
    current = record["current"]
    if (
        record["status"] != "healthy"
        or record["error"] is not None
        or history["action"] != "deploy"
        or history["status"] != "healthy"
        or history["operation"] != record["operation"]
    ):
        raise ValueError("Deployment is unhealthy, rolled back or unverified")
    if not isinstance(current.get("commit"), str) or not re.fullmatch(
        r"[a-f0-9]{40}", current["commit"]
    ):
        raise ValueError("Deployed commit evidence unavailable")
    verified = datetime.fromisoformat(current["verified_at"].replace("Z", "+00:00"))
    if verified.tzinfo is None or not 0 <= now - verified.timestamp() <= DEPLOYMENT_AGE:
        raise ValueError("Deployment verification is stale")
    return dict(current)


def deployment_evidence(repository: RepositoryIdentity) -> dict:
    """Verify target and live checkout drift against fresh manager provenance."""
    from ai_agent.dependency_evidence import git

    state = read_state()
    current = validate_deployment(state, repository.project_name, time.time())
    path = "/opt/" + repository.project_name
    remote = git(path, "remote", "get-url", "origin").removesuffix(".git")
    if (
        remote
        not in {
            f"https://github.com/{repository.github_repository}",
            f"git@github.com:{repository.github_repository}",
        }
        or git(path, "rev-parse", "HEAD") != current["commit"]
    ):
        raise ValueError("Deployed checkout identity drifted")
    if read_state() != state:
        raise ValueError("Deployment changed during verification")
    return current
