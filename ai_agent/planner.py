import re

import anthropic

from ai_agent.config import ANTHROPIC_KEY, ANTHROPIC_MODEL, REPO_PATH
from ai_agent.github_links import enrich_feature_description


client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


IMPLEMENTATION_QUESTION_PATTERNS = (
    "where is",
    "where are",
    "where in the code",
    "current implementation",
    "how is the current",
    "how is the new",
    "how is it calculated",
    "how is the new url",
    "how is the url",
    "what method",
    "what api",
    "which method",
    "which api",
    "what function",
    "which function",
    "stored/accessed",
    "stored or accessed",
    "located in the ui",
    "button located",
)


BUGFIX_SEARCH_KEYWORDS = (
    "cast",
    "casting",
    "channel",
    "proxy",
    "url",
    "toggle",
    "player",
    "session",
    "chromecast",
    "restart",
)


def kotlin_file_sample() -> str:
    files = []
    for path in REPO_PATH.rglob("*.kt"):
        relative = path.relative_to(REPO_PATH)
        if "build" in relative.parts:
            continue
        files.append(str(relative))
        if len(files) >= 50:
            break
    return "\n".join(files)


def codebase_search_context(query: str, max_files: int = 80, max_matches: int = 80) -> str:
    """Return lightweight local repo context so bug triage can be codebase-first.

    This intentionally avoids sending full files to Claude. It provides enough
    filenames and matching lines for the triage/planning step to decide whether
    Codex can inspect and implement the fix without asking the user where code is.
    """
    if not REPO_PATH.exists():
        return f"Repository path does not exist: {REPO_PATH}"

    terms = set(BUGFIX_SEARCH_KEYWORDS)
    terms.update(re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", query.lower()))
    noisy_terms = {
        "the",
        "and",
        "for",
        "with",
        "should",
        "current",
        "when",
        "user",
        "new",
        "bug",
        "fix",
        "active",
    }
    terms = {term for term in terms if term not in noisy_terms}

    files: list[str] = []
    matches: list[str] = []
    extensions = {".kt", ".kts", ".java", ".xml", ".gradle", ".properties"}

    for path in REPO_PATH.rglob("*"):
        if not path.is_file() or path.suffix not in extensions:
            continue
        relative = path.relative_to(REPO_PATH)
        if "build" in relative.parts or ".gradle" in relative.parts:
            continue
        relative_text = str(relative)
        files.append(relative_text)

        if len(matches) >= max_matches:
            continue

        lower_name = relative_text.lower()
        name_matches = any(term in lower_name for term in terms)
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue

        for line_number, line in enumerate(lines, start=1):
            lower_line = line.lower()
            if name_matches or any(term in lower_line for term in terms):
                stripped = line.strip()
                if stripped:
                    matches.append(f"{relative_text}:{line_number}: {stripped[:220]}")
                if len(matches) >= max_matches:
                    break

        if len(files) >= max_files and len(matches) >= max_matches:
            break

    return "Files:\n" + "\n".join(files[:max_files]) + "\n\nRelevant matches:\n" + "\n".join(matches[:max_matches])


def plan_feature(feature_description: str) -> str:
    context = kotlin_file_sample()
    enriched_feature_description = enrich_feature_description(feature_description)

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": f"""
You are a senior Android architect working on a multi-module IPTV app called Channel Cast.

Project modules:
- app/ - Main app module
- data-* - Data layer (storage, network, repository, prefs)
- ui-* - UI layer (features, core, models)
- channel-health-monitor/
- proxy-health-monitor/

Project files sample:
{context}

Feature request: {enriched_feature_description}

Produce:
1. Branch name (feature/xxx)
2. Files to create/modify
3. Step by step implementation plan
4. Ready-to-use Codex prompt
                """,
            }
        ],
    )
    return response.content[0].text


def build_bugfix_prompt(bug_description: str) -> str:
    enriched_bug_description = enrich_feature_description(bug_description)
    search_context = codebase_search_context(enriched_bug_description)
    return f"""
Fix this bug in the Channel Cast Android repository.

Bug report:
{enriched_bug_description}

Local codebase hints from the agent's pre-search:
{search_context}

Requirements:
1. Inspect the existing implementation before changing code.
2. Infer implementation details from the repository instead of asking the user where files, methods, URLs, or state are located.
3. Keep the fix focused on the reported bug.
4. Add or update focused tests when practical.
5. Run the relevant tests or compilation checks available in the repository.
6. Do not make unrelated refactors.
    """.strip()


def assess_bugfix_report(bug_description: str) -> str:
    enriched_bug_description = enrich_feature_description(bug_description)
    search_context = codebase_search_context(enriched_bug_description)

    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=800,
        messages=[
            {
                "role": "user",
                "content": f"""
You are triaging a bug report for an Android coding agent that can search and edit the local repository.

Bug report:
{enriched_bug_description}

Local codebase context:
{search_context}

Decide whether the coding agent can start a focused fix without asking the user for more information.

Important policy:
- Default to READY when the missing details can be discovered by searching the codebase.
- Do NOT ask where a button, method, file, URL builder, state holder, or current implementation is located. The coding agent must inspect the repository for that.
- Do NOT ask what method/API to call unless there are multiple externally visible product behaviors and the codebase cannot disambiguate them.
- Ask questions only for missing product behavior that cannot be inferred from the bug report or code, such as a business rule, UX choice, or acceptance criterion.
- If the bug is reproducible from the description and the expected behavior is clear, return READY.

Return exactly one of these formats:

READY

or

QUESTIONS:
1. <short necessary product/UX question>
2. <short necessary product/UX question>

Ask at most 2 questions.
                """,
            }
        ],
    )
    return response.content[0].text.strip()


def bugfix_questions(assessment: str) -> str | None:
    text = assessment.strip()
    if text.upper().startswith("READY"):
        return None
    if text.upper().startswith("QUESTIONS:"):
        questions = text.split(":", 1)[1].strip()
    else:
        questions = text

    if not questions:
        return "Please provide the missing expected product behavior."

    question_lines = [line.strip() for line in questions.splitlines() if line.strip()]
    product_questions = []
    for line in question_lines:
        normalized = re.sub(r"^\d+[.)]\s*", "", line).strip().lower()
        if any(pattern in normalized for pattern in IMPLEMENTATION_QUESTION_PATTERNS):
            continue
        product_questions.append(line)

    if not product_questions:
        return None

    return "\n".join(product_questions)
