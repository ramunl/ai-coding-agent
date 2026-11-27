"""Validate a pull request URL and pin the refs and SHAs for a merge fix."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from ai_agent.github import ensure_github_configured, github_request
from ai_agent.github_links import extract_urls, parse_github_reference
from ai_agent.projects import active_project


@dataclass(frozen=True)
class MergeConflictTarget:
    """Pin one open same-repository pull request for a merge-conflict fix.

    The refs and SHAs are captured once, before any git write, and the caller
    must reuse this pinned target rather than re-fetching: base and head can
    move while the fix runs, and re-fetching would merge something the owner
    never reviewed.
    """

    number: int
    url: str
    title: str
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    merge_commit_sha: str


class MergeConflictError(ValueError):
    """A PR reference that cannot be used for a merge-conflict fix."""


def _single_pull_reference(text: str) -> tuple[str, int]:
    """Return the (owner/repo, number) of the one PR URL in the text."""
    urls = extract_urls(text)
    if not urls:
        raise MergeConflictError("Send the pull request URL to fix.")
    if len(urls) > 1:
        raise MergeConflictError(
            "Send exactly one pull request URL; found several references."
        )
    reference = parse_github_reference(urls[0])
    if reference is None or reference.kind != "pull" or reference.number is None:
        raise MergeConflictError(f"That is not a pull request URL: {urls[0]}")
    return f"{reference.owner}/{reference.repo}", reference.number


def _same_repo(full_name: object, repository: str) -> bool:
    """Whether a repo name from the API matches the active repository."""
    return bool(full_name) and str(full_name).lower() == repository.lower()


def _branch_tip(repository: str, ref: str) -> str:
    """Read an exact live branch ref; PR payload SHAs can describe older tips."""
    result = github_request(
        "GET", f"/repos/{repository}/git/ref/heads/{quote(ref, safe='/')}"
    )
    if not isinstance(result, dict) or not isinstance(result.get("object"), dict):
        raise MergeConflictError(f"Could not pin the current branch tip for {ref}.")
    obj = result["object"]
    sha = obj.get("sha")
    if (
        result.get("ref") != f"refs/heads/{ref}"
        or obj.get("type") != "commit"
        or not isinstance(sha, str)
        or not re.fullmatch(r"[0-9a-f]{40}", sha)
    ):
        raise MergeConflictError(f"Could not pin the current branch tip for {ref}.")
    return sha


def validate_merge_conflict_pr(text: str) -> MergeConflictTarget:
    """Validate one PR URL and return its pinned base/head refs and SHAs.

    Rejects invalid or ambiguous URLs, other repositories, closed or merged
    pull requests, and forks before any git operation. The returned target is
    meant to be stored and reused so base/head movement after validation cannot
    change what is merged.
    """
    repository, number = _single_pull_reference(text)
    active = active_project().github_repository
    if repository.lower() != active.lower():
        raise MergeConflictError(
            f"That pull request belongs to {repository}, not the active project "
            f"{active}."
        )

    ensure_github_configured()
    pull_data = github_request("GET", f"/repos/{active}/pulls/{number}")

    if pull_data.get("state") != "open":
        raise MergeConflictError(
            f"PR #{number} is not open (state: {pull_data.get('state')})."
        )

    base = pull_data.get("base") or {}
    head = pull_data.get("head") or {}
    base_repo = (base.get("repo") or {}).get("full_name")
    head_repo = (head.get("repo") or {}).get("full_name")
    if not _same_repo(base_repo, active) or not _same_repo(head_repo, active):
        raise MergeConflictError(
            f"PR #{number} is a fork or cross-repository pull request "
            f"(base: {base_repo or 'unknown'}, head: {head_repo or 'unknown'}); "
            f"merge-conflict fixes require a same-repository branch in {active}."
        )

    base_ref = str(base.get("ref") or "")
    head_ref = str(head.get("ref") or "")
    if not (base_ref and head_ref):
        raise MergeConflictError(f"PR #{number} is missing its base or head refs.")

    # GitHub can retain an old base.sha on an open PR after the base branch
    # advances. Resolve both live refs for initial pinning AND revalidation.
    base_sha = _branch_tip(active, base_ref)
    head_sha = _branch_tip(active, head_ref)

    return MergeConflictTarget(
        number=number,
        url=str(pull_data.get("html_url") or ""),
        title=str(pull_data.get("title") or f"PR #{number}"),
        base_ref=base_ref,
        base_sha=base_sha,
        head_ref=head_ref,
        head_sha=head_sha,
        merge_commit_sha=str(pull_data.get("merge_commit_sha") or ""),
    )
