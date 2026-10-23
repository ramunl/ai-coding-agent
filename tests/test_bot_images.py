"""Attachment persistence and owner-only media workflow tests."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests.bot_fixtures import TelegramTestCase


class ImageBotTests(TelegramTestCase):
    def modules(self):
        import sys

        sys.modules["telegram.ext"].CallbackContext = object
        return (
            importlib.import_module("ai_agent.bot.image_intake"),
            importlib.import_module("ai_agent.bot.task_actions"),
        )

    def update(self, group=None, caption="Fix this screen"):
        file = SimpleNamespace(
            file_size=4,
            get_file=AsyncMock(
                return_value=SimpleNamespace(
                    file_path="https://api.telegram.org/file/test"
                )
            ),
        )
        message = SimpleNamespace(
            media_group_id=group,
            message_id=1,
            caption=caption,
            photo=[file],
            document=None,
            reply_text=AsyncMock(),
        )
        return SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=123),
            effective_user=SimpleNamespace(id=123),
        )

    def test_unauthorized_upload_does_not_download(self):
        media, _ = self.modules()
        update = self.update()
        with patch.object(media, "require_authorized", return_value=False):
            asyncio.run(media.media(update, SimpleNamespace()))
        update.message.photo[0].get_file.assert_not_awaited()

    def test_album_one_task_with_caption_and_references(self):
        media, actions = self.modules()
        context = SimpleNamespace(
            user_data={},
            application=SimpleNamespace(mark_data_for_update_persistence=MagicMock()),
        )

        async def run():
            await media.media(self.update("album"), context)
            await media.media(self.update("album", ""), context)
            await asyncio.sleep(0.02)

        with (
            patch.object(media, "ALBUM_WAIT", 0.001),
            patch.object(
                media, "active_project", return_value=SimpleNamespace(name="repo")
            ),
            patch.object(media, "_download", return_value=b"data"),
            patch.object(media, "save", side_effect=[{"id": "one"}, {"id": "two"}]),
            patch.object(media, "report_missing", new=AsyncMock()),
            patch.object(actions, "start", new=AsyncMock()) as start,
        ):
            asyncio.run(run())
        tasks = context.user_data["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["images"], [{"id": "one"}, {"id": "two"}])
        self.assertEqual(tasks[0]["text"], "Fix this screen")
        start.assert_awaited_once()

    def test_invalid_album_rejects_whole_batch_and_cleans(self):
        media, _ = self.modules()
        context = SimpleNamespace(user_data={})

        async def run():
            await media.media(self.update("album"), context)
            await media.media(self.update("album", ""), context)
            await asyncio.sleep(0.02)

        with (
            patch.object(media, "ALBUM_WAIT", 0.001),
            patch.object(
                media, "active_project", return_value=SimpleNamespace(name="repo")
            ),
            patch.object(
                media, "_download", side_effect=[b"data", media.ImageError("bad")]
            ),
            patch.object(media, "save", return_value={"id": "one"}),
            patch.object(media, "remove") as remove,
        ):
            asyncio.run(run())
        self.assertNotIn("tasks", context.user_data)
        remove.assert_called_once_with([{"id": "one"}])

    def test_missing_caption_rejected(self):
        media, _ = self.modules()
        update = self.update(caption="")
        with (
            patch.object(
                media, "active_project", return_value=SimpleNamespace(name="repo")
            ),
            patch.object(media, "_download", return_value=b"data"),
            patch.object(media, "save", return_value={"id": "one"}),
            patch.object(media, "remove") as remove,
        ):
            asyncio.run(media.media(update, SimpleNamespace(user_data={})))
        remove.assert_called_once()
        self.assertIn("caption", update.message.reply_text.await_args.args[0])

    def test_plan_revision_queue_persistence_and_snapshot_are_private(self):
        state = importlib.import_module("ai_agent.bot.state")
        plans = importlib.import_module("ai_agent.plan_state")
        # The persistence tests provide the PTB base classes absent in bot fakes.
        import sys

        ext = sys.modules["telegram.ext"]
        ext.BasePersistence = object
        ext.PersistenceInput = lambda **kwargs: None
        persistence = importlib.import_module("ai_agent.bot.persistence")

        plan = plans.new_plan_state(
            "task", '{"branch":"feature/task","summary":"task","steps":[]}'
        )
        plan.images = [{"id": "opaque", "bytes": 4}]
        revised = plans.revise_plan_state(plan, plan.plan_text)
        context = SimpleNamespace(user_data={"pending_plan": revised})
        state.set_pending_from_plan(context, revised)
        queued = state.enqueue_pending_implementation(
            context, context.user_data["pending_implementation"]
        )
        self.assertEqual(queued["images"], plan.images)
        restored = persistence.deserialize_user_data(
            persistence.serialize_user_data(context.user_data)
        )
        self.assertEqual(restored["task_queue"][0]["images"], plan.images)
        self.assertEqual(restored["pending_plan"].images, plan.images)
        self.assertNotIn("opaque", str(state.snapshot(context.user_data)))

    def test_snapshot_usage_has_only_aggregates(self):
        publisher = importlib.import_module("ai_agent.bot.snapshot_publisher")
        content = publisher.build_content(
            {}, {}, {}, image_storage={"used_bytes": 4, "cap_bytes": 10, "count": 1}
        )
        self.assertEqual(
            content["image_storage"], {"used_bytes": 4, "cap_bytes": 10, "count": 1}
        )
