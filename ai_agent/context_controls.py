"""Bounded context request schemas and durable, single-use confirmations."""

import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ai_agent.context_management import ManagementError, OwnerEvent

CONFIRM_SECONDS = 600
REQUEST_SECONDS = 120
MAX_BINDINGS = 256
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REV = re.compile(r"(?:[1-9][0-9]{0,5}|1000000)")
_SCOPE = re.compile(r"(?:owner|repo:[A-Za-z0-9._-]{1,64})")
_KIND = ("owner", "repository", "initiative", "attempt")
_BASE = {"scope": _SCOPE, "kind": _KIND, "record_id": _ID, "revision": _REV}
CONTEXT_ACTIONS = {
    "context_inspect_v1": dict(_BASE),
    "context_link_v1": {
        **_BASE,
        "target_kind": _KIND,
        "target_id": _ID,
        "target_revision": _REV,
    },
    "context_approve_v1": {**_BASE, "decision_id": _ID},
    "context_reject_v1": {**_BASE, "decision_id": _ID},
}


def authenticate(event: OwnerEvent, owner: str) -> None:
    """Reject untrusted identity before resolving scope or opening an archive."""
    if (
        type(event) is not OwnerEvent
        or not owner
        or event.actor_id != owner
        or event.chat_id != owner
        or type(event.update_id) is not int
        or event.update_id < 0
    ):
        raise ManagementError("Owner authentication required")


def command_args(action: str, args: dict) -> list[str]:
    """Validate exact versioned arguments and convert to existing command syntax."""
    spec = CONTEXT_ACTIONS.get(action)
    if spec is None or type(args) is not dict or set(args) != set(spec):
        raise ManagementError("Invalid context action schema")
    for key, allowed in spec.items():
        value = args[key]
        if type(value) is not str or not (
            value in allowed if isinstance(allowed, tuple) else allowed.fullmatch(value)
        ):
            raise ManagementError("Invalid context action argument")
    return [args[key] for key in spec]


class Confirmations:
    """Transactional bindings outside the inbox and rotating result log.

    A claim commits before archive access. A crash after that point requires
    inspection, even when no archive write actually happened.
    """

    def __init__(self, path: Path) -> None:
        """Select a private database beside the bot state, never inside the inbox."""
        self.path = path

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            self.path.chmod(0o600)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS bindings ("
                "request_id TEXT PRIMARY KEY, token TEXT UNIQUE, "
                "payload TEXT NOT NULL, expires REAL NOT NULL, "
                "consumed INTEGER NOT NULL DEFAULT 0)"
            )
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def offer(
        self,
        request_id: str,
        action: str,
        args: dict,
        scope: dict,
        requested_at: float,
        *,
        now: float | None = None,
    ) -> str:
        """Persist a request once; duplicates never issue another confirmation."""
        command_args(action, args)
        now = time.time() if now is None else now
        if (
            not re.fullmatch(r"[0-9a-f]{8,32}", request_id)
            or type(requested_at) not in (int, float)
            or not 0 < requested_at <= 1_000_000_000_000
            or not 0 <= now - requested_at <= REQUEST_SECONDS
        ):
            raise ManagementError("Invalid or expired context request")
        payload = json.dumps(
            dict(action=action, args=args, scope=scope), sort_keys=True
        )
        token = secrets.token_hex(16)
        with self._transaction() as db:
            # Retain consumed claims until the original request and token expire.
            db.execute("DELETE FROM bindings WHERE expires < ?", (now,))
            if db.execute(
                "SELECT 1 FROM bindings WHERE request_id = ?", (request_id,)
            ).fetchone():
                raise ManagementError(
                    "Request already claimed; inspect before resubmitting"
                )
            if (
                db.execute("SELECT count(*) FROM bindings").fetchone()[0]
                >= MAX_BINDINGS
            ):
                raise ManagementError("Confirmation capacity reached; try later")
            db.execute(
                "INSERT INTO bindings(request_id, token, payload, expires) "
                "VALUES(?,?,?,?)",
                (request_id, token, payload, requested_at + CONFIRM_SECONDS),
            )
        return token

    def consume(
        self,
        token: str,
        action: str,
        event: OwnerEvent,
        owner: str,
        *,
        now: float | None = None,
    ) -> dict:
        """Authenticate and atomically consume an exact action binding."""
        authenticate(event, owner)
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ManagementError("Invalid confirmation")
        now = time.time() if now is None else now
        with self._transaction() as db:
            row = db.execute(
                "SELECT payload, expires, consumed FROM bindings WHERE token = ?",
                (token,),
            ).fetchone()
            if not row or not row[1] - CONFIRM_SECONDS <= now <= row[1] or row[2]:
                raise ManagementError(
                    "Confirmation expired or consumed; inspect before resubmitting"
                )
            payload = json.loads(row[0])
            if payload["action"] != action or payload["scope"]["owner_id"] != owner:
                raise ManagementError("Confirmation does not match action or owner")
            command_args(action, payload["args"])
            db.execute("UPDATE bindings SET consumed = 1 WHERE token = ?", (token,))
        return payload
