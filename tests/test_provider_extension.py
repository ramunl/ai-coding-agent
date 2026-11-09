"""A new provider routes everywhere from its own module and manifest entry.

The fake provider is discovered only through ``tests/fake_manifest.json`` via
the ``AI_AGENT_PROVIDER_MANIFEST`` override, so the production manifest and the
built-in five providers stay untouched. These contract tests prove the provider
appears in the snapshot and role lists, routes planning/implementation/auth/
limits/errors/usage/images through the generic ``ProviderTool``, and that an
unlisted module is never imported while a disabled provider is refused.
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_agent import (
    ai_tools,
    image_delivery,
    planning_provider,
    provider_capabilities,
    provider_limits,
    provider_registry,
    workflow,
)
from tests import fake_provider as fake_module

FAKE_MANIFEST = Path(__file__).with_name("fake_manifest.json")
NAME = fake_module.name


class ProviderExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = dict(os.environ)
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        os.environ["AI_AGENT_PROVIDER_MANIFEST"] = str(FAKE_MANIFEST)
        provider_registry.reset()
        ai_tools.unregister_tool(NAME)

    def tearDown(self) -> None:
        ai_tools.unregister_tool(NAME)
        provider_registry.reset()
        os.environ.clear()
        os.environ.update(self.previous_env)

    def install(self) -> list[str]:
        """Discover the fake manifest and install its generic provider tool."""
        return ai_tools.install_provider_tools()


class DiscoveryContractTests(ProviderExtensionTests):
    def test_fake_provider_appears_in_blocks_and_roles(self) -> None:
        installed = self.install()
        self.assertIn(NAME, installed)
        by_id = {
            block["id"]: block for block in provider_capabilities.provider_blocks()
        }
        self.assertIn(NAME, by_id)
        block = by_id[NAME]
        self.assertEqual(block["status"], "enabled")
        self.assertEqual(block["roles"], ["planner", "executor"])
        self.assertEqual(block["adapter"], ["executor", "planner"])
        self.assertIs(block["images"], True)
        self.assertIs(block["structured_output"], True)
        self.assertEqual(block["usage"], "declared")
        self.assertEqual(
            block["maintenance"],
            {"package": "fake-package", "pinned_version": "0.0.0-test", "gated": False},
        )
        self.assertIn(NAME, ai_tools.tools_for_role("planner"))
        self.assertIn(NAME, ai_tools.tools_for_role("executor"))

    def test_unlisted_module_is_never_imported(self) -> None:
        # The fake manifest lists only the fake provider, so the production
        # modules are unlisted and must not be discovered or imported.
        provider_registry.discover()
        self.assertEqual(provider_registry.known_providers(), [NAME])
        self.assertIsNone(provider_registry.get_provider("claude"))
        self.assertIsNone(provider_registry.get_provider("codex"))
        self.assertIsNone(provider_registry.get_provider("gemini"))

    def test_manifest_rejects_bad_entries_fail_closed(self) -> None:
        cases = {
            "non-dotted": '{"providers": [{"module": "ai_agent/providers/claude"}]}',
            "duplicate": (
                '{"providers": [{"module": "tests.fake_provider"},'
                ' {"module": "tests.fake_provider"}]}'
            ),
            "non-object": '{"providers": [1]}',
            "non-string-package": (
                '{"providers": [{"module": "tests.fake_provider", "package": 5}]}'
            ),
            "non-boolean-gated": (
                '{"providers": [{"module": "tests.fake_provider", "gated": "yes"}]}'
            ),
            "not-a-list": '{"providers": {}}',
            "not-json": "not json",
        }
        for label, content in cases.items():
            with self.subTest(label=label):
                handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
                handle.write(content)
                handle.close()
                try:
                    os.environ["AI_AGENT_PROVIDER_MANIFEST"] = handle.name
                    provider_registry.reset()
                    with self.assertRaises(ValueError):
                        provider_registry.discover()
                finally:
                    os.remove(handle.name)
                    os.environ["AI_AGENT_PROVIDER_MANIFEST"] = str(FAKE_MANIFEST)
                    provider_registry.reset()

    def test_block_skips_maintenance_when_undeclared(self) -> None:
        provider_registry.reset()
        bare = type(
            "Bare", (), {"name": "bare", "enabled": True, "roles": ("planner",)}
        )()
        provider_registry.register("bare", bare)
        block = provider_capabilities.provider_block(bare)
        self.assertNotIn("maintenance", block)


class RoutingContractTests(ProviderExtensionTests):
    def test_planning_routes_through_the_registry(self) -> None:
        self.install()
        self.assertEqual(
            planning_provider.planner_message("p", NAME, "plan.json", 100),
            "fake-plan:plan.json",
        )

    def test_implementation_command_routes_through_the_registry(self) -> None:
        self.install()
        self.assertEqual(
            workflow.implementation_command("do work", NAME),
            ["fake-provider", "run", "do work"],
        )

    def test_image_delivery_routes_through_the_registry(self) -> None:
        self.install()
        with patch.object(
            image_delivery, "attachments", return_value=([("image/png", b"x")], 0)
        ):
            result = image_delivery.run_with_images(
                ["fake-provider", "run", "prompt"], lambda *a, **kw: "ran"
            )
        self.assertEqual(result, ("fake-image", 1, 0))

    def test_limits_snapshot_routes_through_the_registry(self) -> None:
        self.install()
        tool = ai_tools.get_tool(NAME)
        self.assertEqual(tool.limits_key, NAME)
        self.assertEqual(
            provider_limits.limits_snapshot()[NAME], {"status": "ok", "windows": []}
        )

    def test_auth_observation_routes_through_the_registry(self) -> None:
        self.install()
        tool = ai_tools.get_tool(NAME)
        self.assertIn(NAME, [item.name for item in ai_tools.auth_tools()])
        reading = asyncio.run(tool.auth())
        self.assertEqual((reading.status, reading.method), ("authenticated", "oauth"))

    def test_usage_routes_through_the_registry(self) -> None:
        self.install()
        self.assertIn(NAME, ai_tools.token_tools())
        self.assertEqual(ai_tools.get_tool(NAME).tokens("implemented"), 42)
        self.assertEqual(workflow._implementer_tokens(NAME, "implemented"), 42)

    def test_errors_route_through_the_registry(self) -> None:
        self.install()
        tool = ai_tools.get_tool(NAME)
        self.assertIsNotNone(tool.errors)
        self.assertEqual(
            tool.explain_error(RuntimeError("fake-capacity")),
            "Fake provider is at capacity.",
        )
        self.assertIsNone(tool.explain_error(RuntimeError("unrelated")))

    def test_disabled_provider_is_refused(self) -> None:
        fake_module.enabled = False
        self.addCleanup(setattr, fake_module, "enabled", True)
        self.install()
        # Registered but serving no role, exactly like a gated built-in.
        self.assertIn(NAME, ai_tools.known_tools())
        self.assertNotIn(NAME, ai_tools.tools_for_role("executor"))
        self.assertNotIn(NAME, ai_tools.tools_for_role("planner"))
        with self.assertRaises(ValueError):
            provider_capabilities.require_enabled_tool(NAME, "executor")


class ProductionSnapshotTests(ProviderExtensionTests):
    def test_production_snapshot_and_tool_lists_are_unchanged(self) -> None:
        # The override is the only thing that changes discovery; with the real
        # manifest the five built-in providers and enabled role lists stay put.
        os.environ.pop("AI_AGENT_PROVIDER_MANIFEST", None)
        provider_registry.reset()
        ai_tools.unregister_tool(NAME)
        blocks = {
            block["id"]: block for block in provider_capabilities.provider_blocks()
        }
        self.assertEqual(
            set(blocks), {"claude", "codex", "claude-code", "opencode", "deepseek"}
        )
        self.assertNotIn(NAME, ai_tools.known_tools())
        self.assertEqual(
            provider_capabilities.enabled_tools_for_role("planner"),
            ["codex", "claude", "deepseek"],
        )
        self.assertEqual(
            provider_capabilities.enabled_tools_for_role("executor"),
            ["codex", "claude"],
        )


if __name__ == "__main__":
    unittest.main()
