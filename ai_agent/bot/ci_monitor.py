"""Poll CI and describe repair requirements."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ai_agent.bot.transport import reply_chunks, require_authorized
from ai_agent.ci import CiResult, evaluate_ci
from ai_agent.config import (
    CI_FIX_ATTEMPTS,
    CI_POLL_INTERVAL_SECONDS,
    CI_TIMEOUT_SECONDS,
)
from ai_agent.github import ensure_github_configured, github_request
from ai_agent.projects import active_project
from ai_agent_common import choice_keyboard


def build_ci_repair_prompt(original_prompt: str, failure_context: str) -> str:
    """Describe a CI repair while preserving the original task requirements."""
    return f"""
The previous implementation was pushed, but CI failed.

Fix the build/test errors on the current branch.

Original implementation prompt:
{original_prompt}

CI failure context:
{failure_context}

Requirements:
1. Inspect the current branch before editing.
2. Fix only the errors needed to make CI pass.
3. Keep the original requested behavior intact.
4. Add or update focused tests when practical.
5. Run the relevant local build or test command before finishing when available.
6. Do not create a new branch.
    """.strip()


def build_fix_pr_repair_prompt(
    pr_number: int, title: str, body: str, failure_context: str
) -> str:
    """Describe a CI repair using an existing pull request as context."""
    return f"""
An existing GitHub pull request is failing CI.

Fix the build/test errors on the current PR branch.

Pull request:
#{pr_number} {title}

Description:
{body or "(no description)"}

CI failure context:
{failure_context}

Requirements:
1. Inspect the current branch before editing.
2. Fix only the errors needed to make CI pass.
3. Preserve the pull request's intended behavior.
4. Add or update focused tests when practical.
5. Run the relevant local build or test command before finishing when available.
6. Do not create a new branch.
    """.strip()


async def choose_pull_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
) -> None:
    """Offer eligible open pull requests for inspection or repair."""
    try:
        ensure_github_configured()
        repository = active_project().github_repository
        pulls = await asyncio.to_thread(
            github_request,
            "GET",
            f"/repos/{repository}/pulls",
            query={"state": "open", "per_page": 100},
        )
    except RuntimeError as error:
        await reply_chunks(update, f"Could not list pull requests: {error}")
        return
    if command == "fixpr":
        pulls = [
            pr
            for pr in pulls
            if ((pr.get("head") or {}).get("repo") or {}).get("full_name") == repository
        ]
    if not pulls:
        await reply_chunks(
            update, "No eligible open pull requests in the active project."
        )
        return
    await update.message.reply_text(
        "Choose a pull request:",
        reply_markup=choice_keyboard(command, [str(pr["number"]) for pr in pulls]),
    )


async def watch_ci(update: Update, head_sha: str) -> CiResult:
    """Poll CI until completion or timeout and report changed summaries."""
    deadline = asyncio.get_running_loop().time() + CI_TIMEOUT_SECONDS
    last_summary = None

    while True:
        result = await asyncio.to_thread(evaluate_ci, head_sha)
        if result.summary != last_summary:
            message = result.summary
            if result.url:
                message = f"{message}\n{result.url}"
            await reply_chunks(update, message)
            last_summary = result.summary

        if result.state in {"passed", "failed"}:
            return result

        if asyncio.get_running_loop().time() >= deadline:
            await reply_chunks(
                update,
                f"CI polling timed out after {CI_TIMEOUT_SECONDS}s for {head_sha}",
            )
            return result

        await asyncio.sleep(CI_POLL_INTERVAL_SECONDS)


def failed_ci_exhausted_message(ci_result: CiResult, repair_attempt: int) -> str:
    """Explain that CI failed after the configured repair budget."""
    if CI_FIX_ATTEMPTS <= 0:
        message = "CI is still failing. Automatic CI repair is disabled."
    else:
        message = (
            f"CI is still failing after {repair_attempt}/{CI_FIX_ATTEMPTS} repair "
            f"attempts."
        )
    if ci_result.url:
        message = f"{message}\n{ci_result.url}"
    return message


def final_ci_status_message(ci_result: CiResult) -> str:
    """Combine the final CI summary and optional build URL."""
    message = ci_result.summary if ci_result.summary else "CI passed"
    if ci_result.url:
        message = f"{message}\n{ci_result.url}"
    return message


async def ci(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report CI status for the selected pull request."""
    if not require_authorized(update):
        return
    if not context.args:
        await choose_pull_request(update, context, "ci")
        return
    if not context.args[0].isdigit():
        await reply_chunks(update, "Usage: /ci <pr-number>")
        return

    pr_number = int(context.args[0])
    ensure_github_configured()
    pull_data = await asyncio.to_thread(
        github_request,
        "GET",
        f"/repos/{active_project().github_repository}/pulls/{pr_number}",
    )
    head_sha = pull_data["head"]["sha"]
    result = await asyncio.to_thread(evaluate_ci, head_sha)
    message = f"PR #{pr_number}: {result.summary}"
    if result.url:
        message = f"{message}\n{result.url}"
    await reply_chunks(update, message)
