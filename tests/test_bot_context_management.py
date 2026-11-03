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

    def test_invalid_event_rejected_before_scope_or_archive_access(self):
        from ai_agent.context_management import ManagementError, OwnerEvent

        with patch.object(self.module, "_scope") as scope:
            with self.assertRaises(ManagementError):
                self.module._execute(
                    "approve", self.context.args, OwnerEvent("456", "123", 1)
                )
        scope.assert_not_called()

    def test_invalid_update_id_rejected_before_archive_access(self):
        self.update.update_id = True
        with patch.object(self.module, "ContextStore") as factory:
            self.run_command("context_approve")
        factory.assert_not_called()

    def offer(self, action="approve", **changes):
        import time

        args = dict(scope="owner", kind="owner", record_id="notes", revision="1")
        if action == "link":
            args.update(target_kind="owner", target_id="notes", target_revision="1")
        elif action != "inspect":
            args["decision_id"] = "choice"
        args.update(changes)
        prompt = self.module.request_confirmation(
            "abcdef12", f"context_{action}_v1", args, time.time()
        )
        self.context.args = ["confirm", prompt.split()[-1]]

    def test_inbox_confirmation_provenance_restart_and_replay(self):
        with patch.object(self.module, "ContextStore") as access:
            self.offer()
        access.assert_not_called()
        self.run_command("context_approve")
        record = self.store.get("owner", "notes")
        self.assertEqual(record.decisions[0].approval.event_id, "telegram-99")
        self.assertEqual(record.revision, 2)
        with patch.object(self.module, "ContextStore") as access:
            self.run_command("context_approve")
        access.assert_not_called()
        self.assertIn("consumed", self.update.message.reply_text.call_args.args[0])

    def test_edited_or_mismatched_confirmation_cannot_access_archive(self):
        self.offer()
        with patch.object(self.module, "ContextStore") as access:
            self.run_command("context_reject")
            self.update.message.edit_date = 100
            self.run_command("context_approve")
            self.update.message.edit_date = None
            self.update.edited_message = self.update.message
            self.run_command("context_approve")
        access.assert_not_called()
        self.update.edited_message = None
        self.run_command("context_approve")
        self.assertEqual(self.store.get("owner", "notes").revision, 2)

    def test_confirmation_unknown_stale_and_uncertain_failures_are_consumed(self):
        from dataclasses import replace

        for failure in ("unknown", "stale", "unavailable", "uncertain"):
            with self.subTest(failure=failure):
                # Each subcase gets a new request namespace.
                path = Path(self._state_dir) / f"{failure}.db"
                from ai_agent.context_controls import Confirmations

                with patch.object(
                    self.module,
                    "_confirmations",
                    side_effect=lambda: Confirmations(path),
                ):
                    self.offer(
                        record_id="missing" if failure == "unknown" else "notes",
                        revision=str(self.store.get("owner", "notes").revision),
                    )
                    if failure == "stale":
                        self.store.write(
                            replace(self.store.get("owner", "notes"), summary="new"),
                            expected_revision=1,
                        )
                    if failure == "uncertain":
                        write = self.store.write

                        def commit_then_fail(*args, **kwargs):
                            write(*args, **kwargs)
                            raise OSError("/private/archive")

                        with patch.object(
                            self.store, "write", side_effect=commit_then_fail
                        ):
                            self.run_command("context_approve")
                        self.assertEqual(self.store.get("owner", "notes").revision, 3)
                    elif failure == "unavailable":
                        with patch.object(
                            self.store, "get", side_effect=OSError("/private/archive")
                        ):
                            self.run_command("context_approve")
                    else:
                        self.run_command("context_approve")
                    response = self.update.message.reply_text.call_args.args[0]
                    self.assertNotIn("/private", response)
                    self.assertIn(
                        "Revision conflict" if failure == "stale" else "unavailable",
                        response,
                    )
                    with patch.object(self.module, "ContextStore") as access:
                        self.run_command("context_approve")
                    access.assert_not_called()

    def test_confirmed_inspection_link_and_rejection_reuse_services(self):
        from ai_agent.context_controls import Confirmations

        for action in ("inspect", "link", "reject"):
            path = Path(self._state_dir) / f"{action}.db"
            with patch.object(
                self.module, "_confirmations", side_effect=lambda: Confirmations(path)
            ):
                revision = str(self.store.get("owner", "notes").revision)
                self.offer(action, revision=revision)
                self.run_command("context_" + action)
        record = self.store.get("owner", "notes")
        self.assertEqual(record.revision, 3)
        self.assertEqual(record.decisions, ())
        self.assertEqual(record.references[0].record_id, "notes")

    def test_registry_change_refuses_archive_access(self):
        from ai_agent.context_models import RepositoryIdentity

        self.offer()
        changed = Scope(
            "123", RepositoryIdentity("other", "/private/other", "org/other")
        )
        with (
            patch.object(self.module, "_scope", return_value=changed),
            patch.object(self.module, "ContextStore") as archive,
        ):
            self.run_command("context_approve")
        archive.assert_not_called()

    def test_unauthorized_confirmation_cannot_consume_binding(self):
        self.offer()
        with patch.object(self.module, "_confirmations") as claims:
            for user, chat in ((456, 123), (123, 456)):
                self.update.effective_user.id = user
                self.update.effective_chat.id = chat
                self.run_command("context_approve")
        claims.assert_not_called()
        self.update.effective_user.id = self.update.effective_chat.id = 123
        self.run_command("context_approve")
        self.assertEqual(self.store.get("owner", "notes").revision, 2)

    def test_claim_storage_failure_never_accesses_archive(self):
        import sqlite3

        self.offer()
        with (
            patch(
                "ai_agent.context_controls.sqlite3.connect",
                side_effect=sqlite3.OperationalError("/private/path"),
            ),
            patch.object(self.module, "ContextStore") as archive,
        ):
            self.run_command("context_approve")
        archive.assert_not_called()
        response = self.update.message.reply_text.call_args.args[0]
        self.assertIn("unavailable", response)
        self.assertNotIn("/private", response)
