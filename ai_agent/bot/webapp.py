"""Read-only Telegram Mini App dashboard, served from the bot's own process.

Running in-process means the dashboard reads the same state the bot uses
(application.user_data) with no second service and no cross-process locking.
It listens on localhost only; Caddy terminates HTTPS in front of it.

Everything here is best-effort: if the server cannot start, the bot carries on
without a dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from ai_agent.bot.state import snapshot
from ai_agent.bot.webapp_auth import InitDataError, verify_init_data

logger = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("webapp.html")
StateProvider = Callable[[], Awaitable[dict]]
_NO_STORE = {"Cache-Control": "no-store"}
_runner: web.AppRunner | None = None


def _init_data_from(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    return header[4:].strip() if header.lower().startswith("tma ") else ""


def build_web_app(
    state_provider: StateProvider, bot_token: str, owner_id: int
) -> web.Application:
    """HTTP routes. Takes a provider instead of the bot, so it is testable alone."""

    async def page(_request: web.Request) -> web.StreamResponse:
        return web.FileResponse(PAGE, headers=_NO_STORE)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def state(request: web.Request) -> web.Response:
        try:
            verify_init_data(_init_data_from(request), bot_token, owner_id)
        except InitDataError as error:
            return web.json_response(
                {"error": error.reason}, status=error.status, headers=_NO_STORE
            )
        return web.json_response(await state_provider(), headers=_NO_STORE)

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/state", state)
    return app


def _project_and_versions() -> dict:
    """Slow parts (file read, git subprocesses); run off the event loop."""
    from ai_agent.bot.maintenance import core_version_line
    from ai_agent.projects import active_project
    from ai_agent.version import get_runtime_version

    project = active_project()
    return {
        "project": {
            "name": project.name,
            "repository": project.github_repository,
            "branch": project.base_branch,
        },
        "version": get_runtime_version(),
        "core": core_version_line(),
    }


def dashboard_state_provider(ptb_app, owner_id: int) -> StateProvider:
    async def provide() -> dict:
        # Queue state is read on the event-loop thread, where handlers mutate it,
        # so the dashboard sees a consistent picture.
        data = snapshot(ptb_app.user_data.get(owner_id, {}))
        data.update(await asyncio.to_thread(_project_and_versions))
        return data

    return provide


async def start_dashboard(
    ptb_app, bot_token: str, owner_id: int, host: str, port: int
) -> bool:
    """Start serving; return False (and log) instead of raising on any failure."""
    global _runner
    if _runner is not None:
        return True
    app = build_web_app(
        dashboard_state_provider(ptb_app, owner_id), bot_token, owner_id
    )
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
    # Broad on purpose: this runs in post_init, where any exception stops the bot.
    except Exception as error:
        logger.error("Dashboard not started on %s:%s: %s", host, port, error)
        await runner.cleanup()
        return False
    _runner = runner
    logger.info("Dashboard listening on %s:%s", host, port)
    return True


async def stop_dashboard() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
