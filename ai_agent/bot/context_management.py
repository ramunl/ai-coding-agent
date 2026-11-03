"""Explicit owner-only Telegram archive commands, independent of session state."""

import asyncio
from typing import Any

from ai_agent.bot.transport import require_authorized
from ai_agent.config import CHAT_ID
from ai_agent.context_management import ContextManagement, ManagementError, OwnerEvent
from ai_agent.context_models import RecordReference, RepositoryIdentity, Scope
from ai_agent.context_store import (
    ContextPersistenceError,
    ContextStore,
    RevisionConflictError,
)
from ai_agent.context_validation import ContextValidationError
from ai_agent.projects import ProjectError, get_project

USAGE = (
    "Scope: owner or repo:PROJECT (explicit registered project).\n"
    "/context_inspect SCOPE KIND ID REV [KIND ID REV ...]\n"
    "/context_link SCOPE KIND ID REV TARGET_KIND TARGET_ID TARGET_REV\n"
    "/context_approve SCOPE KIND ID REV DECISION_ID\n"
    "/context_reject SCOPE KIND ID REV DECISION_ID"
)


def _scope(value: str) -> Scope:
    if value == "owner":
        return Scope(str(CHAT_ID))
    if not value.startswith("repo:") or not value[5:]:
        raise ManagementError("Explicit scope required")
    project = get_project(value[5:])
    return Scope(
        str(CHAT_ID),
        RepositoryIdentity(
            project.name, str(project.repo_path), project.github_repository
        ),
    )


def _execute(action: str, args: list[str], event: OwnerEvent) -> str:
    if not args or len(args) > 13:
        return USAGE
    scope = _scope(args[0])
    service = ContextManagement(ContextStore(scope), str(CHAT_ID), event)

    def ref(parts: list[str]) -> RecordReference:
        kind, record_id, revision = parts
        return RecordReference(kind, record_id, int(revision), scope)

    if action == "inspect" and len(args) >= 4 and (len(args) - 1) % 3 == 0:
        return service.inspect(
            tuple(ref(args[start : start + 3]) for start in range(1, len(args), 3))
        )
    if action == "link" and len(args) == 7:
        saved = service.link(ref(args[1:4]), ref(args[4:7]))
    elif action in {"approve", "reject"} and len(args) == 5:
        saved = service.decide(ref(args[1:4]), args[4], approve=action == "approve")
    else:
        return USAGE
    return (
        f"Context change saved at revision {saved.revision}. "
        "Inspect before further edits."
    )


async def _command(update: Any, context: Any, action: str) -> None:
    user = getattr(update, "effective_user", None)
    if (
        not require_authorized(update)
        or user is None
        or str(user.id) != str(CHAT_ID)
        or not update.message
        or getattr(update, "edited_message", None)
    ):
        return
    event = OwnerEvent(str(user.id), str(update.effective_chat.id), update.update_id)
    try:
        response = await asyncio.to_thread(_execute, action, context.args, event)
    except RevisionConflictError:
        response = (
            "Revision conflict. Inspect again and explicitly resubmit; "
            "no retry was made."
        )
    except (ContextPersistenceError, OSError):
        response = (
            "Record or archive unavailable. No replacement record was created. "
            "Inspect before retrying."
        )
    except (ContextValidationError, ManagementError, ProjectError, ValueError):
        response = "Invalid context request or scope; no change saved.\n" + USAGE
    await update.message.reply_text(response, parse_mode=None)


async def context_inspect(update: Any, context: Any) -> None:
    """Inspect explicitly pinned records in one scope."""
    await _command(update, context, "inspect")


async def context_link(update: Any, context: Any) -> None:
    """Link explicitly pinned records in one scope."""
    await _command(update, context, "link")


async def context_approve(update: Any, context: Any) -> None:
    """Approve a proposal with provenance from the authenticated Telegram event."""
    await _command(update, context, "approve")


async def context_reject(update: Any, context: Any) -> None:
    """Remove a proposal from the latest revision while preserving its history."""
    await _command(update, context, "reject")
