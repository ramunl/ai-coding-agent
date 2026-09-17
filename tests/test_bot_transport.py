"""Behavior tests for bot transport."""

import importlib

from tests.bot_fixtures import TelegramTestCase


class TransportTests(TelegramTestCase):
    def test_redact_sensitive_replaces_configured_secrets(self) -> None:
        config = importlib.import_module("ai_agent.config")

        redacted = config.redact_sensitive("telegram-secret anthropic-secret visible")

        self.assertEqual(redacted, "[redacted] [redacted] visible")
