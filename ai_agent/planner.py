import anthropic

from ai_agent.config import ANTHROPIC_KEY, ANTHROPIC_MODEL, REPO_PATH
from ai_agent.github_links import enrich_feature_description


client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


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
    return f"""
Fix this bug in the Channel Cast Android repository.

Bug report:
{enriched_bug_description}

Requirements:
1. Inspect the existing implementation before changing code.
2. Keep the fix focused on the reported bug.
3. Add or update focused tests when practical.
4. Run the relevant tests or compilation checks available in the repository.
5. Do not make unrelated refactors.
    """.strip()
