"""Build planning and bug-triage prompts with repository context."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from ai_agent.bugfix_questions import (
    IMPLEMENTATION_QUESTION_PATTERNS,
    bugfix_questions,
)
from ai_agent.context_sources import ContextBlockedError, ContextRequest
from ai_agent.ecosystem import ecosystem_block
from ai_agent.github_links import enrich_feature_description
from ai_agent.image_delivery import invoke
from ai_agent.planning_provider import (
    SCHEMAS_DIR,
    SUPPORTED_PLANNING_AGENTS,
    normalize_planning_agent,
    planner_message,
    planning_agent_label,
)
from ai_agent.project_brief import project_brief_block
from ai_agent.projects import active_project
from ai_agent.repository_context import (
    EXCLUDED_DIR_NAMES,
    SOURCE_EXTENSIONS,
    codebase_search_context,
    repo_file_sample,
)
from ai_agent.rules import rules_prompt_block

__all__ = [
    "EXCLUDED_DIR_NAMES",
    "IMPLEMENTATION_QUESTION_PATTERNS",
    "SCHEMAS_DIR",
    "SOURCE_EXTENSIONS",
    "SUPPORTED_PLANNING_AGENTS",
    "assess_bugfix_report",
    "bugfix_questions",
    "build_bugfix_prompt",
    "codebase_search_context",
    "normalize_planning_agent",
    "plan_feature",
    "planning_agent_label",
    "repo_file_sample",
    "revise_feature_plan",
]


def _validate_context(context: ContextRequest | None) -> None:
    if context is None:
        return
    context.validate()
    repository = context.scope.repository
    project = active_project()
    if (project.name, str(project.repo_path), project.github_repository) != (
        repository.project_name,
        repository.repo_path,
        repository.github_repository,
    ):
        raise ContextBlockedError("Select the context request's original repository")


def plan_feature(
    feature_description: str,
    provider: str | None = None,
    images: list[dict] | None = None,
    *,
    context: ContextRequest | None = None,
) -> str:
    """Request a structured implementation plan with project rules and context."""
    _validate_context(context)
    if images is not None:
        return invoke(
            images,
            partial(plan_feature, context=context),
            feature_description,
            provider,
        )
    file_sample = repo_file_sample()
    enriched_feature_description = enrich_feature_description(feature_description)
    rules_block = (
        (
            project_brief_block(Path(context.scope.repository.repo_path), strict=True)
            if context is not None
            else project_brief_block()
        )
        + ecosystem_block()
        + rules_prompt_block()
    )
    repository = active_project().github_repository

    prompt = f"""
You are a senior software engineer working on the {repository} repository.

Project files sample:
{file_sample}

{rules_block}
Feature request: {enriched_feature_description}

Return only valid JSON with this shape:
{{
  "branch": "feature/short-safe-branch-name",
  "summary": "Short implementation summary",
  "files": ["path/or/File.kt"],
  "steps": ["Step one"],
  "risks": ["Risk to verify"],
  "repositories": [{{"name": "registered-repo", "changes": "What changes there"}}]
}}

Use a branch slug that replaces /, \\, :, ?, *, [, ], (, and ) with -.
In "repositories" list each registered repository the change needs, by its \
name from the REPOSITORIES list, in the order to change them (storage or API \
before the screens that use it). When there are several, start each file and \
step with its repository name.
If the request is an already split or repository-scoped task, list ONLY its \
assigned repository in "repositories". Plan files and steps only for that \
repository. Other parts and prerequisites are context only; mention dependencies \
in risks without planning their changes again.
Keep the JSON compact so it is never truncated: at most 8 files and 8 steps, each \
under 200 characters.
                """
    return planner_message(
        prompt,
        provider,
        "feature_plan.json",
        4000,
        **({"context": context} if context is not None else {}),
    )


def revise_feature_plan(
    feature_description: str,
    current_plan: str,
    feedback: str,
    provider: str | None = None,
    images: list[dict] | None = None,
    *,
    context: ContextRequest | None = None,
) -> str:
    """Request a revised plan using the existing plan and user feedback."""
    _validate_context(context)
    if images is not None:
        return invoke(
            images,
            partial(revise_feature_plan, context=context),
            feature_description,
            current_plan,
            feedback,
            provider,
        )
    enriched_feature_description = enrich_feature_description(feature_description)
    rules_block = (
        (
            project_brief_block(Path(context.scope.repository.repo_path), strict=True)
            if context is not None
            else project_brief_block()
        )
        + ecosystem_block()
        + rules_prompt_block()
    )
    repository = active_project().github_repository

    prompt = f"""
You are revising an implementation plan for the {repository} repository.

{rules_block}
Original feature request:
{enriched_feature_description}

Current plan:
{current_plan}

User feedback:
{feedback}

Return only valid JSON with this shape:
{{
  "branch": "feature/short-safe-branch-name",
  "summary": "Short implementation summary",
  "files": ["path/or/File.kt"],
  "steps": ["Step one"],
  "risks": ["Risk to verify"],
  "repositories": [{{"name": "registered-repo", "changes": "What changes there"}}]
}}

Preserve useful parts of the current plan, incorporate the feedback, and keep the \
change focused.
Use a branch slug that replaces /, \\, :, ?, *, [, ], (, and ) with -.
In "repositories" list each registered repository the change needs, by its \
name from the REPOSITORIES list, in the order to change them (storage or API \
before the screens that use it). When there are several, start each file and \
step with its repository name.
If the request is an already split or repository-scoped task, list ONLY its \
assigned repository in "repositories". Plan files and steps only for that \
repository. Other parts and prerequisites are context only; mention dependencies \
in risks without planning their changes again.
Keep the JSON compact so it is never truncated: at most 8 files and 8 steps, each \
under 200 characters.
                """
    return planner_message(
        prompt,
        provider,
        "feature_plan.json",
        4000,
        **({"context": context} if context is not None else {}),
    )


def build_bugfix_prompt(
    bug_description: str, *, context: ContextRequest | None = None
) -> str:
    """Build an implementation prompt for a reported bug and its triage."""
    _validate_context(context)
    enriched_bug_description = enrich_feature_description(bug_description)
    search_context = codebase_search_context(enriched_bug_description)
    rules_block = (
        (
            project_brief_block(Path(context.scope.repository.repo_path), strict=True)
            if context is not None
            else project_brief_block()
        )
        + ecosystem_block()
        + rules_prompt_block()
    )
    repository = active_project().github_repository
    return f"""
Fix this bug in the {repository} repository.

{rules_block}
Bug report:
{enriched_bug_description}

Local codebase hints from the agent's pre-search:
{search_context}

Requirements:
1. Inspect the existing implementation before changing code.
2. Infer implementation details from the repository instead of asking the user where \
files, methods, URLs, or state are located.
3. Keep the fix focused on the reported bug.
4. Add or update focused tests when practical.
5. Run the relevant tests or compilation checks available in the repository.
6. Do not make unrelated refactors.
    """.strip()


def assess_bugfix_report(
    bug_description: str,
    provider: str | None = None,
    *,
    context: ContextRequest | None = None,
) -> str:
    """Request bug triage with repository context and project rules."""
    _validate_context(context)
    enriched_bug_description = enrich_feature_description(bug_description)
    search_context = codebase_search_context(enriched_bug_description)

    prompt = f"""
You are triaging a bug report for a coding agent that can search and edit the local \
repository.

Bug report:
{enriched_bug_description}

Local codebase context:
{search_context}

Decide whether the coding agent can start a focused fix without asking the user for \
more information.

Important policy:
- Default to ready when the missing details can be discovered by searching the codebase.
- Do NOT ask where a button, method, file, URL builder, state holder, or current \
implementation is located. The coding agent must inspect the repository for that.
- Do NOT ask what method/API to call unless there are multiple externally visible \
product behaviors and the codebase cannot disambiguate them.
- Ask questions only for missing product behavior that cannot be inferred from the \
bug report or code, such as a business rule, UX choice, or acceptance criterion.
- If the bug is reproducible from the description and the expected behavior is \
clear, return ready.

Return valid JSON only. No explanation, no markdown, no prose.

Ready response:
{{"status":"ready","questions":[]}}

Questions response:
{{"status":"questions","questions":["short necessary product/UX question"]}}

Ask at most 2 questions.
                """
    return planner_message(
        prompt,
        provider,
        "bugfix_assessment.json",
        500,
        **({"context": context} if context is not None else {}),
    ).strip()
