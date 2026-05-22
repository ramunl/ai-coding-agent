import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ai_agent.github import github_request


MAX_LINK_CONTEXT_CHARS = 6000
MAX_COMMENTS = 5


@dataclass(frozen=True)
class GitHubReference:
    owner: str
    repo: str
    kind: str
    number: int
    url: str


def find_github_references(text: str) -> list[GitHubReference]:
    references = []
    seen = set()
    for match in re.finditer(r"https?://[^\s<>()]+", text):
        url = match.group(0).rstrip(".,;:!?)]}")
        reference = parse_github_reference(url)
        if not reference:
            continue
        key = (reference.owner, reference.repo, reference.kind, reference.number)
        if key in seen:
            continue
        references.append(reference)
        seen.add(key)
    return references


def parse_github_reference(url: str) -> GitHubReference | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return None

    owner, repo, kind, number_text = parts[:4]
    if kind not in {"issues", "pull"} or not number_text.isdigit():
        return None

    return GitHubReference(owner=owner, repo=repo, kind=kind, number=int(number_text), url=url)


def fetch_github_reference_context(reference: GitHubReference) -> str:
    repo_full_name = f"{reference.owner}/{reference.repo}"
    if reference.kind == "pull":
        item = github_request("GET", f"/repos/{repo_full_name}/pulls/{reference.number}")
        title = item.get("title", "")
        body = item.get("body") or ""
        state = item.get("state", "unknown")
        author = item.get("user", {}).get("login", "unknown")
        header = f"Pull request {repo_full_name}#{reference.number}: {title}"
        extra = [
            f"State: {state}",
            f"Author: {author}",
            f"Base: {item.get('base', {}).get('ref', 'unknown')}",
            f"Head: {item.get('head', {}).get('ref', 'unknown')}",
        ]
    else:
        item = github_request("GET", f"/repos/{repo_full_name}/issues/{reference.number}")
        title = item.get("title", "")
        body = item.get("body") or ""
        state = item.get("state", "unknown")
        author = item.get("user", {}).get("login", "unknown")
        header = f"Issue {repo_full_name}#{reference.number}: {title}"
        extra = [f"State: {state}", f"Author: {author}"]

    comments = github_request(
        "GET",
        f"/repos/{repo_full_name}/issues/{reference.number}/comments",
        query={"per_page": str(MAX_COMMENTS)},
    )
    comment_lines = []
    if isinstance(comments, list):
        for comment in comments[:MAX_COMMENTS]:
            commenter = comment.get("user", {}).get("login", "unknown")
            comment_body = comment.get("body") or ""
            comment_lines.append(f"- {commenter}: {truncate(comment_body, 800)}")

    sections = [header, reference.url, *extra, "", "Body:", body or "(empty)"]
    if comment_lines:
        sections.extend(["", f"Recent comments ({len(comment_lines)}):", *comment_lines])
    return truncate("\n".join(sections), MAX_LINK_CONTEXT_CHARS)


def build_github_links_context(text: str) -> str:
    sections = []
    for reference in find_github_references(text):
        try:
            sections.append(fetch_github_reference_context(reference))
        except Exception as exc:
            sections.append(f"Could not fetch {reference.url}: {exc}")
    return "\n\n---\n\n".join(sections)


def enrich_feature_description(feature_description: str) -> str:
    link_context = build_github_links_context(feature_description)
    if not link_context:
        return feature_description
    return f"{feature_description}\n\nGitHub link context:\n{link_context}"


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 15].rstrip() + "\n...[truncated]"
