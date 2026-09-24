"""Mini App dashboard: initData auth, HTTP routes, and fail-safe startup."""

import importlib
import json
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from ai_agent.bot import webapp
from ai_agent.bot.webapp_auth import InitDataError, sign_init_data, verify_init_data

TOKEN = "123:ABC"
OWNER = 777


def _init_data(
    user_id: int = OWNER, auth_date: int | None = None, token: str = TOKEN
) -> str:
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Roman"}),
    }
    return sign_init_data(fields, token)


class InitDataTests(unittest.TestCase):
    def test_accepts_genuine_fresh_owner_data(self) -> None:
        self.assertEqual(verify_init_data(_init_data(), TOKEN, OWNER)["id"], OWNER)

    def _rejects(self, init_data: str, reason: str, status: int = 401) -> None:
        with self.assertRaises(InitDataError) as caught:
            verify_init_data(init_data, TOKEN, OWNER)
        self.assertIn(reason, caught.exception.reason)
        self.assertEqual(caught.exception.status, status)

    def test_rejects_missing(self) -> None:
        self._rejects("", "missing")

    def test_rejects_tampered_field(self) -> None:
        self._rejects(_init_data().replace("Roman", "Mallory"), "invalid signature")

    def test_rejects_data_signed_for_another_bot(self) -> None:
        self._rejects(_init_data(token="999:OTHER"), "invalid signature")

    def test_rejects_other_telegram_user(self) -> None:
        self._rejects(_init_data(user_id=999), "not the bot owner", status=403)

    def test_rejects_stale_data(self) -> None:
        self._rejects(_init_data(auth_date=int(time.time()) - 2 * 86400), "expired")

    def test_rejects_malformed(self) -> None:
        self._rejects("not=a=query&&", "malformed")


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = AsyncMock(return_value={"queue": [{"id": 1}], "running": None})
        app = webapp.build_web_app(self.provider, TOKEN, OWNER)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_state_requires_initdata(self) -> None:
        response = await self.client.get("/api/state")
        self.assertEqual(response.status, 401)
        self.provider.assert_not_awaited()

    async def test_state_rejects_other_user(self) -> None:
        response = await self.client.get(
            "/api/state", headers={"Authorization": "tma " + _init_data(user_id=1)}
        )
        self.assertEqual(response.status, 403)
        self.provider.assert_not_awaited()

    async def test_state_serves_owner_without_caching(self) -> None:
        response = await self.client.get(
            "/api/state", headers={"Authorization": "tma " + _init_data()}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["queue"], [{"id": 1}])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_page_and_health_need_no_auth_and_carry_no_state(self) -> None:
        page = await self.client.get("/")
        self.assertEqual(page.status, 200)
        self.assertIn("telegram-web-app.js", await page.text())
        health = await self.client.get("/healthz")
        self.assertEqual(await health.json(), {"ok": True})
        self.provider.assert_not_awaited()


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_combines_owner_state_with_project_and_versions(self) -> None:
        ptb_app = types.SimpleNamespace(
            user_data={
                OWNER: {
                    "task_queue": [
                        {"id": 2, "branch_name": "feature/a", "codex_prompt": "SECRET"}
                    ]
                },
                999: {"task_queue": [{"id": 9, "branch_name": "not-mine"}]},
            }
        )
        meta = {
            "project": {"name": "channel-cast"},
            "version": "v1",
            "core": "core: v1.0",
        }
        with patch.object(webapp, "_project_and_versions", return_value=meta):
            state = await webapp.dashboard_state_provider(ptb_app, OWNER)()
        self.assertEqual([task["branch"] for task in state["queue"]], ["feature/a"])
        self.assertEqual(state["project"]["name"], "channel-cast")
        self.assertNotIn("SECRET", json.dumps(state))


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_port_conflict_returns_false_instead_of_crashing(self) -> None:
        import asyncio

        blocker = await asyncio.start_server(
            lambda reader, writer: None, "127.0.0.1", 0
        )
        port = blocker.sockets[0].getsockname()[1]
        try:
            started = await webapp.start_dashboard(
                types.SimpleNamespace(user_data={}), TOKEN, OWNER, "127.0.0.1", port
            )
        finally:
            blocker.close()
        self.assertFalse(started)

    async def test_unexpected_startup_error_returns_false(self) -> None:
        with patch.object(
            webapp.web.TCPSite, "start", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            started = await webapp.start_dashboard(
                types.SimpleNamespace(user_data={}), TOKEN, OWNER, "127.0.0.1", 0
            )
        self.assertFalse(started)

    async def _run_start_hook(
        self, url: str, started: bool = True, port: int = 8787, chat_id: int = OWNER
    ):
        telegram_bot = importlib.import_module("ai_agent.telegram_bot")
        app = types.SimpleNamespace(
            bot=types.SimpleNamespace(set_chat_menu_button=AsyncMock())
        )
        with (
            patch.object(telegram_bot, "WEBAPP_URL", url),
            patch.object(telegram_bot, "WEBAPP_PORT", port),
            patch.object(telegram_bot, "CHAT_ID", chat_id),
            patch.object(
                webapp, "start_dashboard", AsyncMock(return_value=started)
            ) as start,
        ):
            await telegram_bot._start_dashboard(app)
        return start, app.bot.set_chat_menu_button

    async def test_disabled_when_url_not_configured(self) -> None:
        start, menu = await self._run_start_hook("")
        start.assert_not_awaited()
        menu.assert_not_awaited()

    async def test_refuses_plain_http(self) -> None:
        start, menu = await self._run_start_hook("http://1-2-3-4.sslip.io")
        start.assert_not_awaited()
        menu.assert_not_awaited()

    async def test_disabled_when_port_invalid(self) -> None:
        start, menu = await self._run_start_hook("https://1-2-3-4.sslip.io", port=0)
        start.assert_not_awaited()
        menu.assert_not_awaited()

    async def test_disabled_for_group_chat_id(self) -> None:
        start, menu = await self._run_start_hook(
            "https://1-2-3-4.sslip.io", chat_id=-100123
        )
        start.assert_not_awaited()
        menu.assert_not_awaited()

    async def test_sets_menu_button_when_server_starts(self) -> None:
        start, menu = await self._run_start_hook("https://1-2-3-4.sslip.io")
        start.assert_awaited_once()
        button = menu.await_args.kwargs["menu_button"]
        self.assertEqual(button.web_app.url, "https://1-2-3-4.sslip.io")
        self.assertEqual(button.text, "Dashboard")

    async def test_no_menu_button_when_server_failed(self) -> None:
        _, menu = await self._run_start_hook("https://1-2-3-4.sslip.io", started=False)
        menu.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
