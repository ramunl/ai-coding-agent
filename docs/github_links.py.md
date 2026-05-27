# github_links.py

**Purpose**: Extract, parse, and enrich links in user input with contextual information.

## Overview
This module identifies GitHub and web links in text, fetches relevant context from them, and enriches feature/bug descriptions with that context for better AI understanding.

## Data Structures

### `GitHubReference` (dataclass)
Represents a parsed GitHub URL.

**Fields**:
- `owner: str` - Repository owner (username/org)
- `repo: str` - Repository name
- `kind: str` - Type: "issues", "pull", "commit", "blob"
- `url: str` - Original URL
- `number: int | None` - Issue/PR number (for issues/pull)
- `ref: str | None` - Branch/tag name (for blob)
- `path: str | None` - File path (for blob)
- `sha: str | None` - Commit SHA (for commit)

### `WebReference` (dataclass)
Represents a parsed web URL.

**Fields**:
- `url: str` - Full URL
- `domain: str` - Domain (lowercase)

## Link Discovery Functions

### `extract_urls(text: str) -> list[str]`
Finds all URLs in text using regex.

**Returns**: List of unique URLs
- Matches http:// and https:// URLs
- Stops at common boundary characters: space, <, >, (, ), and punctuation
- Removes trailing punctuation from URL ends

### `find_github_references(text: str) -> list[GitHubReference]`
Finds all GitHub links in text.

**Returns**: List of unique GitHubReference objects
- Deduplicates based on owner, repo, kind, number, ref, path, sha

### `find_web_references(text: str) -> list[WebReference]`
Finds all allowed web links in text (excluding GitHub).

**Returns**: List of WebReference objects for allowed domains
- Filters by `LINK_ALLOWED_DOMAINS` configuration

## GitHub URL Parsing

### `parse_github_reference(url: str) -> GitHubReference | None`
Parses GitHub URLs into structured data.

**Supported URL Formats**:

1. **Issues**: `https://github.com/owner/repo/issues/123`
   - Returns: `kind="issues"`, `number=123`

2. **Pull Requests**: `https://github.com/owner/repo/pull/456`
   - Returns: `kind="pull"`, `number=456`

3. **Commits**: `https://github.com/owner/repo/commit/abc123def456`
   - SHA must be 7-40 hex characters
   - Returns: `kind="commit"`, `sha="abc123def456"`

4. **File Blob**: `https://github.com/owner/repo/blob/main/src/file.kt`
   - Returns: `kind="blob"`, `ref="main"`, `path="src/file.kt"`

**Returns**: `None` if URL doesn't match supported formats

## Context Fetching Functions

### `fetch_github_reference_context(reference: GitHubReference) -> str`
Fetches detailed context for a GitHub reference.

**For Issues/PRs**:
- Title, body, state, author
- Base/head branch info (for PRs)
- Recent comments (up to 5)

**For Commits**:
- Commit message
- Author name
- List of changed files (up to 20)

**For Files (blob)**:
- File size
- Complete file content
- Branch/ref information

**Max size**: 6000 characters (truncated if larger)

### `fetch_issue_comments(repo_full_name: str, number: int) -> list[dict]`
Fetches recent comments on an issue or PR.

**Limit**: Up to 5 most recent comments

### `format_issue_like_context(header: str, url: str, extra: list[str], body: str, comments: list[dict]) -> str`
Formats issue/PR information nicely.

**Includes**:
- Header and URL
- Extra metadata lines
- Issue/PR body
- Recent comments with author names

### `fetch_web_reference_context(reference: WebReference) -> str`
Fetches content from web URLs.

**For HTML**:
- Extracts title from <title> tag
- Extracts main text content
- Skips script/style/noscript tags

**For Other Content-Types**:
- Returns raw content

**Limits**:
- Max 500KB to download
- Max 6000 characters of extracted text

## Utilities

### `is_allowed_web_domain(domain: str) -> bool`
Checks if a domain is in the allowed list.

**Matches**:
- Exact domain match
- Subdomain of allowed domain (example.com matches *.example.com)

### `truncate(text: str, max_chars: int) -> str`
Truncates text and adds ellipsis if too long.

**Returns**: Original text or truncated version with "...[truncated]" suffix

## Main Integration Functions

### `build_link_context(text: str) -> str`
Extracts and fetches context for all links in text.

**Returns**: Formatted markdown with:
- All GitHub reference contexts
- All web reference contexts
- Error messages for failed fetches
- Sections separated by "---" delimiter

### `build_github_links_context(text: str) -> str`
Alias for `build_link_context`.

### `enrich_feature_description(feature_description: str) -> str`
Enriches a feature description with link context.

**Returns**: Original description plus link context if any links found
- Format: "Original description\n\nLink context:\n{contexts}"

## Constants

- `MAX_LINK_CONTEXT_CHARS`: 6000 - Max chars per link context
- `MAX_COMMENTS`: 5 - Max comments to fetch
- `MAX_FILE_CHARS`: 12000 - Max file content chars
- `MAX_WEB_BYTES`: 500000 - Max bytes to download
- `MAX_WEB_TEXT_CHARS`: 6000 - Max extracted web text chars

## Usage Example
```python
from ai_agent.github_links import enrich_feature_description

feature = "Add feature as in https://github.com/owner/repo/issues/123"
enriched = enrich_feature_description(feature)
print(enriched)
# Shows feature description + full issue context
```
