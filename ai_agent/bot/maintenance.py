"""Handle deployment, versions, and shared-core maintenance."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes

from ai_agent.bot.constants import CORE_TARGETS, DEPLOY_TARGET_ALIASES, DEPLOY_TARGETS
from ai_agent.bot.transport import (
    prompt_for_arguments,
    reply_chunks,
    require_authorized,
)
from ai_agent.config import CHAT_ID
from ai_agent.core_maintenance import bump_core_isolated
from ai_agent.projects import active_project
from ai_agent.shell import run
from ai_agent.test_runner import run_unit_tests
from ai_agent.version import get_runtime_version
from ai_agent.workflow import validate_branch_name
from ai_agent_common import CoreCommand, choice_keyboard

try:
    from ai_agent_common import create_release
except ImportError:
    create_release = None

logger = logging.getLogger(__name__)


async def _notify_core_drift_on_startup(app: Application) -> None:
    """On boot, message the owner only if this bot's core is behind latest.

    Best-effort: the check itself never raises (outdated_notice swallows
    errors), and we guard the send too, so nothing here can stop startup.
    """
    notice = await asyncio.to_thread(_core_command.outdated_notice)
    if notice:
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=notice)
        except Exception as error:
            logger.warning("Could not send core-drift notice (ignored): %s", error)


def _deployable_names() -> str:
    """Deployable projects by repo name, so messages follow the target table."""
    names = (str(target["repo"].name) for target in DEPLOY_TARGETS.values())
    return ", ".join(sorted(names))


async def deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deploy the requested branch of the active fleet project."""
    if not require_authorized(update):
        return

    if len(context.args) != 1:
        await reply_chunks(
            update,
            "Usage: /deploy <branch>\n"
            f"Deploys the active project: {_deployable_names()}.",
        )
        return

    project = active_project()
    target_key = DEPLOY_TARGET_ALIASES.get(project.name.lower())
    if target_key is None:
        repository_name = project.github_repository.rsplit("/", 1)[-1].lower()
        target_key = DEPLOY_TARGET_ALIASES.get(repository_name)
    if target_key is None:
        await reply_chunks(
            update,
            (
                f"The active project '{project.name}' is not deployable. Choose "
                f"one of: {_deployable_names()} with /repo_use."
            ),
        )
        return
    target = DEPLOY_TARGETS[target_key]

    branch = context.args[0]
    try:
        validate_branch_name(branch)
    except ValueError as error:
        await reply_chunks(update, str(error))
        return

    try:
        note = await asyncio.to_thread(_submit_deployment, target, branch)
    except (RuntimeError, ValueError) as error:
        await reply_chunks(update, f"Deploy failed: {error}")
        return
    await reply_chunks(update, note)


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the agent unit tests and report their output."""
    if not require_authorized(update):
        return

    await reply_chunks(update, "Running agent unit tests...")
    result = await asyncio.to_thread(run_unit_tests)
    await reply_chunks(update, result)


async def _on_core_update_tap(
    update: Update, context: ContextTypes.DEFAULT_TYPE, target: str
) -> None:
    if not require_authorized(update):
        return
    await update.callback_query.edit_message_text(f"Updating core for {target}...")
    result = await asyncio.to_thread(_core_update_target, target)
    await update.callback_query.edit_message_text(result)


_CORE_ROOT = Path(__file__).resolve().parents[2]


_core_command = CoreCommand(
    submodule_dir=_CORE_ROOT / "ai_agent_common",
    superproject_dir=_CORE_ROOT,
    submodule_path="ai_agent_common",
    agent_name="ai-coding-agent",
)


def core_version_line() -> str:
    """One local-only line for /version. Never hits the network."""
    return _core_command.short_line()


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report the running agent version from the shared core."""
    if not require_authorized(update):
        return
    await reply_chunks(update, get_runtime_version())


def _submit_deployment(target: dict, ref: str) -> str:
    """Queue an independent supervised job rather than restarting this bot."""
    try:
        result = run(
            ["/usr/local/sbin/ai-deploy", "submit", "deploy", target["repo"].name, ref],
            cwd=Path("/opt"),
            timeout=30,
        )
    except OSError as error:
        raise RuntimeError(
            "Deployment manager is not installed; "
            "follow the Ops installation instructions"
        ) from error
    payload = json.loads(result.output)
    if not isinstance(payload, dict) or payload.get("status") != "queued":
        raise ValueError("Deployment manager did not queue the operation")
    return (
        f"Deploy queued for {target['label']}: {payload.get('operation', 'unknown')}. "
        "Use the Ops bot /deployments to check the result."
    )


def _run_target_deploy(target: dict) -> str:
    """Queue the updated core pin through the central deployment manager."""
    return _submit_deployment(target, "main")


def _core_update_target(target_name: str) -> str:
    """Hub action: bump one bot's core pin to latest, then deploy that bot.

    Prepare the pin in an isolated checkout, push it, then queue deployment.
    The running checkout remains unchanged until the manager verifies it.
    """
    target = CORE_TARGETS.get(DEPLOY_TARGET_ALIASES.get(target_name, target_name))
    if target is None:
        known = ", ".join(sorted(CORE_TARGETS))
        return f"Unknown bot '{target_name}'. Known: {known}"

    repo = target["repo"]
    try:
        changed, message = bump_core_isolated(repo)
    except (OSError, RuntimeError, ValueError) as error:
        return f"{target['label']}: Core update failed: {error}"
    if not changed:
        return f"{target['label']}: {message}"

    try:
        deploy_note = _run_target_deploy(target)
    except (RuntimeError, ValueError) as error:
        return f"{target['label']}: {message}\nDeployment was not queued: {error}"
    return f"{target['label']}: {message}\n{deploy_note}"


async def core(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspect, update, or release the shared core using command arguments."""
    if not require_authorized(update):
        return
    wants_release = bool(context.args) and context.args[0] == "release"
    if wants_release:
        if create_release is None:
            await reply_chunks(
                update,
                "Core release support is unavailable because ai-agent-common "
                "is not synced. Redeploy with submodules enabled.",
            )
            return
        version = context.args[1] if len(context.args) > 1 else None
        note = " ".join(context.args[2:]) if len(context.args) > 2 else ""
        if version is None or not note:
            await prompt_for_arguments(
                update,
                context,
                "core release",
                (
                    "Enter a version and release note, e.g. v2.0 added inline "
                    "button helpers."
                ),
            )
            return
        await reply_chunks(update, f"Releasing core {version}...")
        ok, message = await asyncio.to_thread(
            create_release, _CORE_ROOT / "ai_agent_common", version, note
        )
        await reply_chunks(update, message)
        return

    wants_update = bool(context.args) and context.args[0] == "update"
    if wants_update:
        target_name = context.args[1] if len(context.args) > 1 else None
        if target_name is None:
            await update.message.reply_text(
                "Tap the bot whose shared core should be updated:",
                reply_markup=choice_keyboard("core_update", sorted(CORE_TARGETS)),
            )
            return
        await reply_chunks(update, f"Updating core for {target_name}...")
        result = await asyncio.to_thread(_core_update_target, target_name)
        await reply_chunks(update, result)
    else:
        await reply_chunks(update, "Checking core version...")
        result = await asyncio.to_thread(_core_command.status_text)
        await reply_chunks(update, result)
