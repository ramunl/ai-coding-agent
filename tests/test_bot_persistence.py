"""Restart-safety of bot state: what survives, what must not, and robustness."""

import asyncio
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path

from ai_agent.bot import state
from ai_agent.bot.persistence import (
    JsonStatePersistence,
    read_state_file,
    serialize_user_data,
)
from ai_agent.plan_state import ExecutionState, PlanState

USER = 12345


def _plan(**overrides) -> PlanState:
    values = dict(
        id="p1",
        feature="genre filter",
        revision=2,
        plan_text="{}",
        approved=True,
        history=["v1"],
    )
    values.update(overrides)
    return PlanState(**values)


def _task(task_id: int, branch: str) -> dict:
    return {
        "id": task_id,
        "change": "c",
        "codex_prompt": "secret prompt",
        "branch_name": branch,
        "confirmation_label": "implementation",
        "implementation_agent": "codex",
    }


class RestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "sub" / "state.json"

    def _restart(self, user_data: dict) -> dict:
        """Save through one persistence instance, load through a fresh one."""
        asyncio.run(JsonStatePersistence(self.path).update_user_data(USER, user_data))
        return asyncio.run(JsonStatePersistence(self.path).get_user_data())[USER]

    def test_queue_plan_and_choices_survive_restart(self) -> None:
        execution = ExecutionState(
            branch="feature/x",
            files_changed=["A.kt"],
            diff_summary="1 file",
            full_diff="diff",
            logs="log",
            pr_url="https://pr/1",
            tests="PASSED",
        )
        restored = self._restart(
            {
                "task_queue": [_task(1, "feature/a"), _task(2, "feature/b")],
                "next_task_id": 3,
                "pending_plan": _plan(),
                "planning_agent": "claude",
                "implementation_agent": "codex",
                "verbosity": "debug",
                "last_execution": execution,
            }
        )
        self.assertEqual(
            [t["branch_name"] for t in restored["task_queue"]],
            ["feature/a", "feature/b"],
        )
        self.assertEqual(restored["next_task_id"], 3)
        self.assertEqual(restored["pending_plan"], _plan())
        self.assertIsInstance(restored["pending_plan"], PlanState)
        self.assertEqual(restored["last_execution"], execution)
        self.assertEqual(restored["planning_agent"], "claude")

    def test_restart_mid_task_does_not_jam_the_queue(self) -> None:
        # The bot is killed while a task runs: runner flag and active execution
        # are set. After restart neither may come back, or the queue would
        # never run again and /fixpr would report a phantom running task.
        restored = self._restart(
            {
                "task_queue": [_task(2, "feature/b")],
                "queue_runner_active": True,
                "active_execution": {"branch": "feature/a", "phase": "Polling CI"},
                "argument_prompt": (99, "plan"),
            }
        )
        self.assertNotIn("queue_runner_active", restored)
        self.assertNotIn("active_execution", restored)
        self.assertNotIn("argument_prompt", restored)
        self.assertIsNone(state.active_execution_text(restored))
        self.assertEqual(len(state.task_queue(restored)), 1)

    def test_transient_keys_never_reach_disk(self) -> None:
        self._restart(
            {"queue_runner_active": True, "active_execution": {"branch": "x"}}
        )
        text = self.path.read_text()
        self.assertNotIn("queue_runner_active", text)
        self.assertNotIn("active_execution", text)

    def test_transient_key_in_old_file_is_ignored_on_load(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "users": {
                        str(USER): {"queue_runner_active": True, "verbosity": "normal"}
                    },
                }
            )
        )
        restored = asyncio.run(JsonStatePersistence(self.path).get_user_data())[USER]
        self.assertNotIn("queue_runner_active", restored)
        self.assertEqual(restored["verbosity"], "normal")


class RobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "state.json"

    def _write(self, users: dict) -> None:
        self.path.write_text(json.dumps({"version": 1, "users": users}))

    def test_missing_file_is_empty_state(self) -> None:
        self.assertEqual(
            asyncio.run(JsonStatePersistence(self.path).get_user_data()), {}
        )

    def test_corrupt_file_starts_empty_and_is_kept_aside(self) -> None:
        self.path.write_text("{ not json")
        self.assertEqual(read_state_file(self.path), {})
        self.assertTrue(self.path.with_name("state.json.corrupt").exists())

    def test_saved_dataclass_survives_field_changes(self) -> None:
        # A field removed from PlanState since saving is ignored; a field added
        # since saving falls back to its default. Pickle would fail both ways.
        self._write(
            {
                str(USER): {
                    "pending_plan": {
                        "__dataclass__": "PlanState",
                        "fields": {
                            "id": "p1",
                            "feature": "f",
                            "revision": 1,
                            "plan_text": "t",
                            "approved": False,
                            "field_from_old_version": "x",
                        },
                    }
                }
            }
        )
        restored = asyncio.run(JsonStatePersistence(self.path).get_user_data())[USER]
        self.assertEqual(restored["pending_plan"].history, [])

    def test_unreadable_key_is_dropped_not_fatal(self) -> None:
        self._write(
            {
                str(USER): {
                    "pending_plan": {
                        "__dataclass__": "ClassThatNoLongerExists",
                        "fields": {},
                    },
                    "verbosity": "concise",
                }
            }
        )
        restored = asyncio.run(JsonStatePersistence(self.path).get_user_data())[USER]
        self.assertNotIn("pending_plan", restored)
        self.assertEqual(restored["verbosity"], "concise")

    def test_unserializable_value_is_skipped_not_fatal(self) -> None:
        encoded = serialize_user_data(
            {"verbosity": "debug", "last_execution": object()}
        )
        self.assertEqual(encoded, {"verbosity": "debug"})

    def test_file_is_owner_only_and_write_is_atomic(self) -> None:
        asyncio.run(
            JsonStatePersistence(self.path).update_user_data(
                USER, {"verbosity": "debug"}
            )
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        leftovers = [
            p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])


class ReadModelTests(unittest.TestCase):
    def test_state_functions_accept_a_plain_mapping(self) -> None:
        data = {"task_queue": [_task(1, "feature/a")], "verbosity": "debug"}
        self.assertEqual(len(state.task_queue(data)), 1)
        self.assertEqual(state.get_verbosity(data).value, "debug")

    def test_snapshot_is_json_and_hides_bulky_or_sensitive_fields(self) -> None:
        data = {
            "task_queue": [_task(1, "feature/a")],
            "pending_plan": _plan(),
            "active_execution": {
                "branch": "feature/z",
                "phase": "Polling CI",
                "status": "RUNNING",
            },
            "last_execution": ExecutionState(
                branch="b",
                files_changed=["A.kt"],
                diff_summary="s",
                full_diff="FULL DIFF",
                logs="LOGS",
                pr_url=None,
            ),
        }
        snap = state.snapshot(data)
        text = json.dumps(snap)
        self.assertEqual(snap["queue"][0]["branch"], "feature/a")
        self.assertEqual(snap["pending_plan"]["revision"], 2)
        self.assertEqual(snap["running"]["phase"], "Polling CI")
        for hidden in ("secret prompt", "FULL DIFF", "LOGS"):
            self.assertNotIn(hidden, text)

    def test_snapshot_of_empty_state(self) -> None:
        snap = state.snapshot({})
        self.assertEqual(snap["queue"], [])
        self.assertIsNone(snap["running"])
        self.assertIsNone(snap["pending_plan"])


class KeyClassificationTests(unittest.TestCase):
    def test_every_user_data_key_in_the_code_is_classified(self) -> None:
        """A new key must be declared persistent or transient in state.py.

        Guards the failure this module exists to prevent: an unclassified
        runtime flag being restored after a restart and jamming the bot.
        """
        root = Path(__file__).resolve().parent.parent / "ai_agent"
        pattern = re.compile(
            r'(?:user_data|_data\(context\))\s*(?:\.get\(|\.pop\(|\.setdefault\(|\[)\s*"([a-z_]+)"'
        )
        used = set()
        for source in root.rglob("*.py"):
            used.update(pattern.findall(source.read_text(encoding="utf-8")))
        self.assertTrue(used, "pattern found no keys; the check itself is broken")
        unclassified = used - state.PERSISTENT_KEYS - state.TRANSIENT_KEYS
        self.assertEqual(
            unclassified, set(), f"classify in bot/state.py: {sorted(unclassified)}"
        )
        self.assertEqual(state.PERSISTENT_KEYS & state.TRANSIENT_KEYS, frozenset())


if __name__ == "__main__":
    unittest.main()


class PtbIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_application_restores_state_across_restart(self) -> None:
        """Drive python-telegram-bot's own save/load cycle, not just our class."""
        from unittest.mock import AsyncMock, patch

        from telegram import Bot
        from telegram.ext import Application

        path = Path(tempfile.mkdtemp()) / "state.json"
        with (
            patch.object(Bot, "initialize", AsyncMock()),
            patch.object(Bot, "shutdown", AsyncMock()),
        ):
            first = (
                Application.builder()
                .token("1:A")
                .persistence(JsonStatePersistence(path))
                .build()
            )
            await first.initialize()
            first._user_data[USER]["task_queue"] = [_task(1, "feature/a")]
            first._user_data[USER]["queue_runner_active"] = True
            first._user_ids_to_be_updated_in_persistence.add(USER)
            await first.update_persistence()
            await first.shutdown()

            restarted = (
                Application.builder()
                .token("1:A")
                .persistence(JsonStatePersistence(path))
                .build()
            )
            await restarted.initialize()
            restored = restarted.user_data[USER]
            await restarted.shutdown()

        self.assertEqual(restored["task_queue"][0]["branch_name"], "feature/a")
        self.assertNotIn("queue_runner_active", restored)
