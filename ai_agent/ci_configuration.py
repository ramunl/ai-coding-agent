"""Positively distinguish missing Actions configuration from delayed CI."""

import re

from ai_agent.github import github_request

_SHA = re.compile(r"[0-9a-f]{40}")


def _get(path: str, query: dict | None = None) -> dict:
    value = github_request("GET", path, query=query, timeout=10, max_bytes=1024 * 1024)
    if not isinstance(value, dict):
        raise ValueError("CI configuration evidence is incomplete")
    return value


def _tree(repository: str, sha: str) -> list[dict]:
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ValueError("CI configuration tree identity is invalid")
    value = _get(f"/repos/{repository}/git/trees/{sha}")
    entries = value.get("tree")
    if (
        value.get("sha") != sha
        or value.get("truncated") is not False
        or not isinstance(entries, list)
        or any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or entry.get("type") not in {"tree", "blob", "commit"}
            or not isinstance(entry.get("sha"), str)
            or not _SHA.fullmatch(entry["sha"])
            for entry in entries
        )
    ):
        raise ValueError("CI configuration tree is unavailable or incomplete")
    return entries


def no_actions_workflows(repository: str, head_sha: str) -> bool:
    """Require an empty workflow registry AND no workflow files at the PR head.

    Never infer absence from a timeout, denied API access, disabled workflows,
    a truncated tree or an empty run list alone. A first workflow introduced in
    the PR still counts as configured before GitHub registers or starts it.
    """
    workflows = _get(f"/repos/{repository}/actions/workflows", {"per_page": 1})
    count, entries = workflows.get("total_count"), workflows.get("workflows")
    if type(count) is not int or count < 0 or not isinstance(entries, list):
        raise ValueError("Actions workflow configuration is unavailable")
    if count:
        return False
    if entries or not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha):
        raise ValueError("Actions workflow configuration identity is invalid")
    commit = _get(f"/repos/{repository}/git/commits/{head_sha}")
    if commit.get("sha") != head_sha or not isinstance(commit.get("tree"), dict):
        raise ValueError("PR head cannot be verified for CI configuration")
    tree = commit["tree"].get("sha")
    for directory in (".github", "workflows"):
        children = _tree(repository, tree)
        matches = [entry for entry in children if entry["path"] == directory]
        if not matches:
            return True
        if len(matches) != 1 or matches[0]["type"] != "tree":
            return False  # Unusual layout: never silently bypass checks.
        tree = matches[0]["sha"]
    return not any(
        entry["type"] != "blob" or entry["path"].lower().endswith((".yml", ".yaml"))
        for entry in _tree(repository, tree)
    )
