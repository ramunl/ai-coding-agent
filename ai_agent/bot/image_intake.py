"""Owner-only Telegram images and bounded album collection into tasks."""

from __future__ import annotations

import asyncio
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ai_agent.bot.transport import reply_chunks, require_authorized
from ai_agent.config import IMAGE_MAX_BYTES, IMAGE_MAX_PER_TASK
from ai_agent.image_delivery import WARNING
from ai_agent.projects import active_project
from ai_agent.task_images import ImageError, remove, resolve, save
from ai_agent.tasks import add_task

ALBUM_WAIT = 2.0
ALBUM_LIFETIME = 15.0
_gate = asyncio.Lock()


@dataclass
class Album:
    """Keep only opaque stored references while an album is arriving."""

    repo: str
    created: float = field(default_factory=time.monotonic)
    images: list[dict] = field(default_factory=list)
    caption: str = ""
    error: str = ""
    timer: asyncio.Task | None = None


_albums: dict[str, Album] = {}
_closed: dict[str, float] = {}


def _download(url: str) -> bytes:
    """Read at most the limit plus one byte; never log the credentialed URL."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "api.telegram.org":
        raise ImageError("Unsupported Telegram file endpoint.")
    try:
        # Telegram's official file endpoint does not require redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args: Any, **kwargs: Any) -> None:
                return None

        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(url, timeout=30) as response:
            data = response.read(IMAGE_MAX_BYTES + 1)
        if len(data) > IMAGE_MAX_BYTES:
            raise ImageError("Image exceeds the byte limit.")
        return data
    except (OSError, ValueError) as error:
        raise ImageError("Telegram image download failed or was incomplete.") from error


async def report_missing(update: Any, images: list[dict]) -> None:
    """Visibly report missing attachments without stopping otherwise valid work."""
    if images:
        _, missing = await asyncio.to_thread(resolve, images)
        if missing:
            await reply_chunks(update, WARNING)


async def media(update: Any, context: Any) -> None:
    """Accept an owner's photo/document; a caption becomes the task text."""
    if not require_authorized(update):
        return
    message = update.message
    group = str(message.media_group_id or f"single-{message.message_id}")
    async with _gate:
        now = time.monotonic()
        for key, at in list(_closed.items()):
            if now - at > 600:
                _closed.pop(key)
        if group in _closed:
            await reply_chunks(
                update, "Late album image rejected; resend as a new task."
            )
            return
        album = _albums.get(group)
        if album is None:
            if len(_albums) >= 4:
                await reply_chunks(
                    update, "Too many uploads in progress; retry shortly."
                )
                return
            album = _albums[group] = Album(active_project().name)
        if album.error:
            await reply_chunks(
                update, "This album was rejected; resend as a new upload."
            )
            return
        if album.timer:
            album.timer.cancel()
        try:
            caption = (message.caption or "").strip()
            if caption:
                if album.caption and album.caption != caption:
                    raise ImageError("Album captions conflict; use one task caption.")
                album.caption = caption
            if len(album.images) >= IMAGE_MAX_PER_TASK:
                raise ImageError(
                    "Too many task images; the entire upload was rejected."
                )
            if now - album.created > ALBUM_LIFETIME:
                raise ImageError("Album was incomplete or took too long; resend it.")
            file = message.photo[-1] if message.photo else message.document
            if not file or not file.file_size or file.file_size > IMAGE_MAX_BYTES:
                raise ImageError("Image size is unknown or exceeds the byte limit.")
            remote = await file.get_file()
            data = await asyncio.to_thread(_download, remote.file_path)
            if len(data) != file.file_size:
                raise ImageError("Image download was incomplete.")
            if not album.error:
                album.images.append(await asyncio.to_thread(save, data))
        except ImageError as error:
            album.error = str(error)
        except OSError:
            album.error = "Image storage unavailable; resend the upload."
        except Exception:
            # Telegram exceptions can contain credentialed download URLs.
            album.error = "Telegram image download failed; resend the upload."
        if message.media_group_id:
            album.timer = asyncio.create_task(_finish_later(group, update, context))
        else:
            await _finish(group, update, context)


async def _finish_later(group: str, update: Any, context: Any) -> None:
    await asyncio.sleep(ALBUM_WAIT)
    async with _gate:
        await _finish(group, update, context)


async def _finish(group: str, update: Any, context: Any) -> None:
    album = _albums.pop(group)
    _closed[group] = time.monotonic()
    if not album.caption or len(album.caption) > 4000:
        album.error = "Upload needs a task caption of 1–4000 characters."
    if album.error:
        await asyncio.to_thread(remove, album.images)
        await reply_chunks(update, album.error)
        return
    task = add_task(context.user_data, album.caption, album.repo, None, album.images)
    context.application.mark_data_for_update_persistence(
        user_ids=update.effective_user.id
    )
    await report_missing(update, album.images)
    await reply_chunks(
        update,
        f"Task added with {len(album.images)} image(s). "
        "Albums close after 2 seconds without another image; "
        "check the count and resend if incomplete.",
    )
    # Import here to avoid the planning -> intake -> task_actions cycle.
    from ai_agent.actions import ActionError
    from ai_agent.bot.task_actions import start

    try:
        await start(context.application, update.effective_user.id, task)
    except ActionError:
        await reply_chunks(update, "Task is waiting in To do.")
