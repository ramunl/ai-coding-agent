import base64
import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse
import urllib.error
import urllib.request

from ai_agent.config import COMMAND_TIMEOUT_SECONDS, LINK_ALLOWED_DOMAINS
from ai_agent.github import github_request


MAX_LINK_CONTEXT_CHARS = 6000
MAX_COMMENTS = 5
MAX_FILE_CHARS = 12000
MAX_WEB_BYTES = 500_000
MAX_WEB_TEXT_CHARS = 6000


@dataclass(frozen=True)
class GitHubReference:
    owner: str
    repo: str
    kind: str
    url: str
    number: int | None = None
    ref: str | None = None
    path: str | None = None
    sha: str | None = None


@dataclass(frozen=True)
class WebReference:
    url: str
    domain: str


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = " ".join(html.unescape(data).split())
        if not text:
            return
        if self._in_title:
            self.title = f"{self.title} {text}".strip()
        elif not self._skip_depth:
            self.parts.append(text)

    def text(self) -> str:
        return "\n".join(self.parts)


def extract_urls(text: str) -> list[str]:
    urls = []
    seen = set()
    for match in re.finditer(r"https?://[^\s<>()]+", text):
        url = match.group(0).rstrip(".,;:!?)]}")
        if url in seen:
            continue
        urls.append(url)
        seen.add(url)
    return urls


def find_github_references(text: str) -> list[GitHubReference]:
    references = []
    seen = set()
    for url in extract_urls(text):
        reference = parse_github_reference(url)
        if not reference:
            continue
        key = (reference.owner, reference.repo, reference.kind, reference.number, reference.ref, reference.path, reference.sha)
        if key in seen:
            continue
        references.append(reference)
        seen.add(key)
    return references


def find_web_references(text: str) -> list[WebReference]:
    references = []
    for url in extract_urls(text):
        if parse_github_reference(url):
            continue
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if is_allowed_web_domain(domain):
            references.append(WebReference(url=url, domain=domain))
    return references


def is_allowed_web_domain(domain: str) -> bool:
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in LINK_ALLOWED_DOMAINS)


def parse_github_reference(url: str) -> GitHubReference | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return None

    owner, repo, kind = parts[:3]
    if kind in {"issues", "pull"}:
        number_text = parts[3]
        if not number_text.isdigit():
            return None
        return GitHubReference(owner=owner, repo=repo, kind=kind, number=int(number_text), url=url)

    if kind == "commit":
        sha = parts[3]
        if not re.fullmatch(r"[A-Fa-f0-9]{7,40}", sha):
            return None
        return GitHubReference(owner=owner, repo=repo, kind=kind, sha=sha, url=url)

    if kind == "blob" and len(parts) >= 5:
        ref = parts[3]
        path = "/".join(parts[4:])
        return GitHubReference(owner=owner, repo=repo, kind=kind, ref=ref, path=path, url=url)

    return None


def fetch_github_reference_context(reference: GitHubReference) -> str:
    repo_full_name = f"{reference.owner}/{reference.repo}"
    if reference.kind == "pull":
        if reference.number is None:
            raise ValueError("Pull request reference is missing number")
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
        comments = fetch_issue_comments(repo_full_name, reference.number)
        return format_issue_like_context(header, reference.url, extra, body, comments)

    if reference.kind == "issues":
        if reference.number is None:
            raise ValueError("Issue reference is missing number")
        item = github_request("GET", f"/repos/{repo_full_name}/issues/{reference.number}")
        title = item.get("title", "")
        body = item.get("body") or ""
        state = item.get("state", "unknown")
        author = item.get("user", {}).get("login", "unknown")
        header = f"Issue {repo_full_name}#{reference.number}: {title}"
        extra = [f"State: {state}", f"Author: {author}"]
        comments = fetch_issue_comments(repo_full_name, reference.number)
        return format_issue_like_context(header, reference.url, extra, body, comments)

    if reference.kind == "commit":
        if not reference.sha:
            raise ValueError("Commit reference is missing SHA")
        commit = github_request("GET", f"/repos/{repo_full_name}/commits/{reference.sha}")
        message = commit.get("commit", {}).get("message", "")
        author = commit.get("commit", {}).get("author", {}).get("name", "unknown")
        files = commit.get("files", [])
        file_lines = [
            f"- {file.get('filename', 'unknown')} ({file.get('status', 'modified')}, +{file.get('additions', 0)}/-{file.get('deletions', 0)})"
            for file in files[:20]
        ]
        sections = [
            f"Commit {repo_full_name}@{reference.sha}",
            reference.url,
            f"Author: {author}",
            "",
            "Message:",
            message or "(empty)",
        ]
        if file_lines:
            sections.extend(["", f"Changed files ({len(files)} total):", *file_lines])
        return truncate("\n".join(sections), MAX_LINK_CONTEXT_CHARS)

    if reference.kind == "blob":
        if not reference.path or not reference.ref:
            raise ValueError("File reference is missing path or ref")
        content = github_request(
            "GET",
            f"/repos/{repo_full_name}/contents/{reference.path}",
            query={"ref": reference.ref},
        )
        encoded = content.get("content", "")
        encoding = content.get("encoding", "")
        if encoding == "base64":
            file_text = base64.b64decode(encoded).decode("utf-8", errors="replace")
        else:
            file_text = str(encoded)
        sections = [
            f"File {repo_full_name}/{reference.path}",
            reference.url,
            f"Ref: {reference.ref}",
            f"Size: {content.get('size', 'unknown')} bytes",
            "",
            "Content:",
            truncate(file_text, MAX_FILE_CHARS),
        ]
        return truncate("\n".join(sections), MAX_LINK_CONTEXT_CHARS)

    raise ValueError(f"Unsupported GitHub reference kind: {reference.kind}")


def fetch_issue_comments(repo_full_name: str, number: int) -> list[dict]:
    comments = github_request(
        "GET",
        f"/repos/{repo_full_name}/issues/{number}/comments",
        query={"per_page": str(MAX_COMMENTS)},
    )
    return comments if isinstance(comments, list) else []


def format_issue_like_context(header: str, url: str, extra: list[str], body: str, comments: list[dict]) -> str:
    comment_lines = []
    for comment in comments[:MAX_COMMENTS]:
        commenter = comment.get("user", {}).get("login", "unknown")
        comment_body = comment.get("body") or ""
        comment_lines.append(f"- {commenter}: {truncate(comment_body, 800)}")

    sections = [header, url, *extra, "", "Body:", body or "(empty)"]
    if comment_lines:
        sections.extend(["", f"Recent comments ({len(comment_lines)}):", *comment_lines])
    return truncate("\n".join(sections), MAX_LINK_CONTEXT_CHARS)


def fetch_web_reference_context(reference: WebReference) -> str:
    request = urllib.request.Request(
        reference.url,
        headers={"User-Agent": "channel-cast-ai-agent"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=COMMAND_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("content-type", "")
            body = response.read(MAX_WEB_BYTES).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        error_body = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {error_body}") from exc

    if "html" in content_type:
        extractor = TextExtractor()
        extractor.feed(body)
        title = extractor.title or reference.url
        text = extractor.text()
    else:
        title = reference.url
        text = body

    sections = [
        f"Web page: {title}",
        reference.url,
        f"Content-Type: {content_type or 'unknown'}",
        "",
        "Text:",
        truncate(text, MAX_WEB_TEXT_CHARS),
    ]
    return truncate("\n".join(sections), MAX_LINK_CONTEXT_CHARS)


def build_link_context(text: str) -> str:
    sections = []
    for reference in find_github_references(text):
        try:
            sections.append(fetch_github_reference_context(reference))
        except Exception as exc:
            sections.append(f"Could not fetch {reference.url}: {exc}")
    for reference in find_web_references(text):
        try:
            sections.append(fetch_web_reference_context(reference))
        except Exception as exc:
            sections.append(f"Could not fetch {reference.url}: {exc}")
    return "\n\n---\n\n".join(sections)


def build_github_links_context(text: str) -> str:
    return build_link_context(text)


def enrich_feature_description(feature_description: str) -> str:
    link_context = build_link_context(feature_description)
    if not link_context:
        return feature_description
    return f"{feature_description}\n\nLink context:\n{link_context}"


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 15].rstrip() + "\n...[truncated]"
