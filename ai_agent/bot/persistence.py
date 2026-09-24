"""Restart-safe storage for per-user bot state, as a JSON file.

Plugged into python-telegram-bot's persistence hook, so every handler keeps
using context.user_data unchanged — it simply survives restarts now.

Why JSON and not PTB's PicklePersistence: pickle records each class's import
path, so the next refactor that moves PlanState would make saved state
unloadable. Here dataclasses are tagged by name and rebuilt from the fields
the current code knows, so old files survive code changes, and the file is
readable when debugging.

Only keys in state.PERSISTENT_KEYS are written. Runtime flags such as
queue_runner_active are never saved: restoring one after a restart would jam
the queue permanently.

Saving is best-effort: a failed write is logged, never raised, because losing
a state snapshot is far better than crashing the bot.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any

from telegram.ext import BasePersistence, PersistenceInput

from ai_agent.bot.atomic_file import write_json_atomic
from ai_agent.bot.state import PERSISTENT_KEYS
from ai_agent.plan_state import ExecutionState, PlanState

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
_TAG = "__dataclass__"
_DATACLASSES = {cls.__name__: cls for cls in (PlanState, ExecutionState)}


# ---------------------------------------------------------------- encoding


def encode_value(value: Any) -> Any:
    """Convert a state value into JSON-compatible data."""
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        if name not in _DATACLASSES:
            raise TypeError(f"unregistered dataclass {name}")
        return {_TAG: name, "fields": asdict(value)}
    if isinstance(value, dict):
        return {str(key): encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"cannot persist {type(value).__name__}")


def decode_value(value: Any) -> Any:
    """Rebuild a state value, tolerating fields added or removed since saving."""
    if isinstance(value, dict):
        if _TAG in value:
            cls = _DATACLASSES[value[_TAG]]
            known = {field.name for field in fields(cls)}
            saved = value.get("fields") or {}
            return cls(**{key: item for key, item in saved.items() if key in known})
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def serialize_user_data(data: dict) -> dict:
    """Encode only persistent keys; skip (and log) anything that cannot be saved."""
    encoded = {}
    for key in sorted(PERSISTENT_KEYS):
        if key not in data:
            continue
        try:
            value = encode_value(data[key])
            json.dumps(value)
        except (TypeError, ValueError) as error:
            logger.warning("Not persisting state key %s: %s", key, error)
            continue
        encoded[key] = value
    return encoded


def deserialize_user_data(raw: dict) -> dict:
    """Decode saved keys; drop unknown or unreadable ones instead of failing."""
    data = {}
    for key, value in raw.items():
        if key not in PERSISTENT_KEYS:
            continue  # e.g. a transient flag written by an older version
        try:
            data[key] = decode_value(value)
        except (KeyError, TypeError) as error:
            logger.warning("Dropping unreadable saved state key %s: %s", key, error)
    return data


# ---------------------------------------------------------------- file I/O


def read_state_file(path: Path) -> dict[int, dict]:
    """Return {user_id: encoded user data}; an absent or corrupt file yields {}."""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        users = payload["users"]
        return {int(user_id): dict(data) for user_id, data in users.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        logger.error("State file %s is unreadable (%s); starting empty", path, error)
        with contextlib.suppress(OSError):
            path.replace(path.with_name(path.name + ".corrupt"))
        return {}


def write_state_file(path: Path, users: dict[int, dict]) -> None:
    """Persist all users' encoded state."""
    write_json_atomic(
        path,
        {
            "version": FORMAT_VERSION,
            "users": {str(user_id): data for user_id, data in users.items()},
        },
    )


# ---------------------------------------------------------------- PTB hook


class JsonStatePersistence(BasePersistence):
    """Persist user_data only; chat, bot, and callback data are not used."""

    def __init__(self, path: Path, update_interval: float = 5) -> None:
        super().__init__(
            store_data=PersistenceInput(
                bot_data=False, chat_data=False, user_data=True, callback_data=False
            ),
            update_interval=update_interval,
        )
        self.path = Path(path)
        self._users: dict[int, dict] | None = None

    def _encoded_users(self) -> dict[int, dict]:
        if self._users is None:
            self._users = read_state_file(self.path)
        return self._users

    async def get_user_data(self) -> dict[int, dict]:
        return {
            user_id: deserialize_user_data(data)
            for user_id, data in self._encoded_users().items()
        }

    async def update_user_data(self, user_id: int, data: dict) -> None:
        # PTB calls this every update_interval for users whose data changed.
        # Write through immediately so an unclean kill loses seconds, not all.
        self._encoded_users()[user_id] = serialize_user_data(data)
        write_state_file(self.path, self._encoded_users())

    async def drop_user_data(self, user_id: int) -> None:
        self._encoded_users().pop(user_id, None)
        write_state_file(self.path, self._encoded_users())

    async def refresh_user_data(self, user_id: int, user_data: dict) -> None:
        return None

    async def flush(self) -> None:
        if self._users is not None:
            write_state_file(self.path, self._users)

    # ---- unused stores: PTB requires these methods to exist

    async def get_chat_data(self) -> dict:
        return {}

    async def update_chat_data(self, chat_id: int, data: dict) -> None:
        return None

    async def drop_chat_data(self, chat_id: int) -> None:
        return None

    async def refresh_chat_data(self, chat_id: int, chat_data: dict) -> None:
        return None

    async def get_bot_data(self) -> dict:
        return {}

    async def update_bot_data(self, data: dict) -> None:
        return None

    async def refresh_bot_data(self, bot_data: dict) -> None:
        return None

    async def get_callback_data(self) -> None:
        return None

    async def update_callback_data(self, data: Any) -> None:
        return None

    async def get_conversations(self, name: str) -> dict:
        return {}

    async def update_conversation(
        self, name: str, key: tuple, new_state: object | None
    ) -> None:
        return None
