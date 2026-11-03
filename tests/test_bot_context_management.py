"""Owner Telegram controls use real storage without session or active scope."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ai_agent.context_models import Decision, OwnerContext, Scope
from ai_agent.context_store import ContextStore
from tests.bot_fixtures import TelegramTestCase


class ContextCommandsTest(TelegramTestCase):
    def setUp(self):
        super().setUp()
        from ai_agent.bot import context_management

        self.module = context_management
        self.store = ContextStore(Scope("123"), Path(self._state_dir) / "management")
        self.store.write(
            OwnerContext(
                record_id="notes",
                scope=Scope("123"),
                summary="Notes",
                decisions=(Decision("choice", "Proposal"),),
            ),
            expected_revision=0,
        )
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=123),
            update_id=99,
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        self.context = SimpleNamespace(
            args=["owner", "owner", "notes", "1", "choice"], user_data={}
        )
        self.factory = patch.object(
            self.module, "ContextStore", return_value=self.store
        )
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def run_command(self, command):
        asyncio.run(getattr(self.module, command)(self.update, self.context))

    def test_approval_provenance_and_stale_rejection(self):
        self.run_command("context_approve")
        record = self.store.get("owner", "notes")
        self.assertEqual(record.decisions[0].approval.event_id, "telegram-99")
        self.run_command("context_reject")
        self.assertIn(
            "Revision conflict", self.update.message.reply_text.call_args.args[0]
        )
        self.assertEqual(self.context.user_data, {})

    def test_unauthorized_sender_and_chat_cannot_mutate(self):
        for user, chat in ((456, 123), (123, 456)):
            self.update.effective_user.id = user
            self.update.effective_chat.id = chat
            self.run_command("context_approve")
        self.update.message.reply_text.assert_not_called()
        self.assertEqual(self.store.get("owner", "notes").revision, 1)

    def test_inspect_unknown_and_invalid_scope_are_safe(self):
        self.context.args = ["owner", "owner", "missing", "1"]
        self.run_command("context_inspect")
        text = self.update.message.reply_text.call_args.args[0]
        self.assertIn("unavailable", text)
        self.assertNotIn(self._state_dir, text)
        self.context.args = ["active", "owner", "notes", "1"]
        self.run_command("context_inspect")
        self.assertIn(
            "Invalid context", self.update.message.reply_text.call_args.args[0]
        )

    def test_controls_registered(self):
        from ai_agent.telegram_bot import command_handlers

        for action in ("inspect", "link", "approve", "reject"):
            self.assertIs(
                command_handlers()["context_" + action],
                getattr(self.module, "context_" + action),
            )

    def test_rejection_and_link_commands(self):
        self.run_command("context_reject")
        self.assertEqual(self.store.get("owner", "notes").decisions, ())
        self.context.args = ["owner", "owner", "notes", "2", "owner", "notes", "1"]
        self.run_command("context_link")
        self.assertEqual(self.store.get("owner", "notes").references[0].revision, 1)

    def test_repository_selection_is_explicit(self):
        project = SimpleNamespace(
            name="repo", repo_path=Path("/private/repo"), github_repository="org/repo"
        )
        with patch.object(self.module, "get_project", return_value=project) as lookup:
            scope = self.module._scope("repo:repo")
        lookup.assert_called_once_with("repo")
        self.assertEqual(scope.repository.repo_path, "/private/repo")
        self.assertEqual(scope.owner_id, "123")

    def test_unavailable_archive_response_hides_exception_paths(self):
        self.context.args = ["owner", "owner", "notes", "1"]
        with patch.object(self.store, "get", side_effect=OSError("/private/archive")):
            self.run_command("context_inspect")
        response = self.update.message.reply_text.call_args.args[0]
        self.assertIn("unavailable", response)
        self.assertNotIn("/private", response)
