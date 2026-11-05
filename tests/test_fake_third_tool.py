"""A third tool routes everywhere from its own module, not a central dispatch."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from ai_agent import (
    image_delivery,
    planning_provider,
    provider_limits,
    tool_auth,
    workflow,
)
from ai_agent.ai_tools import get_tool, tools_for_role
from ai_agent.tool_auth_providers import Reading

from tests import fake_third_tool as fake


class FakeThirdToolTests(unittest.TestCase):
    def setUp(self) -> None:
        fake.install()

    def tearDown(self) -> None:
        fake.remove()

    def test_registry_knows_the_fake_roles(self) -> None:
        self.assertIn(fake.NAME, tools_for_role("planner"))
        self.assertIn(fake.NAME, tools_for_role("executor"))

    def test_planning_routes_through_the_registry(self) -> None:
        self.assertEqual(
            planning_provider.planner_message("p", fake.NAME, "plan.json", 100),
            "fake-plan:plan.json",
        )

    def test_implementation_command_routes_through_the_registry(self) -> None:
        self.assertEqual(
            workflow.implementation_command("do work", fake.NAME),
            [fake.NAME, "run", "do work"],
        )

    def test_image_delivery_routes_through_the_registry(self) -> None:
        with patch.object(
            image_delivery, "attachments", return_value=([("image/png", b"x")], 0)
        ):
            result = image_delivery.run_with_images(
                [fake.NAME, "run", "prompt"], lambda *a, **kw: "ran"
            )
        self.assertEqual(result, ("fake-image", 1, 0))

    def test_limits_snapshot_routes_through_the_registry(self) -> None:
        self.assertEqual(
            provider_limits.limits_snapshot()[fake.LIMITS_KEY],
            {"status": "ok", "windows": []},
        )

    def test_auth_observations_route_through_the_registry(self) -> None:
        cache = tool_auth.ToolAuth()
        self.assertIn(fake.NAME, cache.snapshot())
        tool = get_tool(fake.NAME)
        with patch.object(
            tool, "auth", new=AsyncMock(return_value=Reading("authenticated", "oauth"))
        ):
            asyncio.run(cache.refresh())
        self.assertEqual(cache.snapshot()[fake.NAME]["status"], "authenticated")
