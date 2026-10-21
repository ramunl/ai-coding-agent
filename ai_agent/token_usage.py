"""Count provider calls per day, role and tool, for the dashboard.

Only dates, roles, tool names and integer counts are stored or published.
Prompts, outputs and identifiers never reach this module: callers pass a token
total, or None when the provider reported none. None is counted as an unknown
call, never as zero tokens.

The store is one small JSON file holding at most RETENTION_DAYS of buckets. It
is rewritten atomically under an exclusive lock, so two writers cannot lose an
update. See docs/token_usage.md for the schema.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ai_agent.bot.atomic_file import write_json_atomic
from ai_agent.config import TOKEN_USAGE_PATH

logger = logging.getLogger(__name__)

FORMAT = 1
RETENTION_DAYS = 90
ROLES = ("planner", "implementer")
TOOLS = ("codex", "claude")
BUCKET_FIELDS = ("calls", "unknown", "tokens")
# Codex prints "tokens used" and the count on the same line or the next one.
_CODEX_TOKENS = re.compile(r"tokens used[:\s]*([0-9][0-9,._]*)", re.IGNORECASE)
_last_omission: str | None = None


def utc_day(now: datetime | None = None) -> str:
    """Return the UTC calendar day (YYYY-MM-DD) a call is counted under."""
    return (now or datetime.now(UTC)).date().isoformat()


@dataclass(frozen=True)
class UsageRecord:
    """One provider call: its role, tool, UTC day and reported token total."""

    role: str
    tool: str
    tokens: int | None
    day: str = field(default_factory=utc_day)

    def __post_init__(self) -> None:
        """Reject values outside the published vocabulary before storing them."""
        if self.role not in ROLES or self.tool not in TOOLS:
            raise ValueError(f"Unknown usage role or tool: {self.role}/{self.tool}")
        if self.tokens is not None and not _is_count(self.tokens):
            raise ValueError("Token totals must be non-negative integers or None")
        date.fromisoformat(self.day)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def codex_total_tokens(output: str) -> int | None:
    """Return the total Codex printed after "tokens used", or None if it printed none.

    The format is not pinned by any fixture in this repository; if the installed
    CLI prints something else, every call is counted as unknown, never as zero.
    """
    matches = _CODEX_TOKENS.findall(output or "")
    if not matches:
        return None
    return int(re.sub(r"[,._]", "", matches[-1]))


def claude_total_tokens(usage: object) -> int | None:
    """Return input plus output tokens from an Anthropic usage object, or None.

    Cache reads and writes are reported separately and are not included.
    """
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if _is_count(input_tokens) and _is_count(output_tokens):
        return input_tokens + output_tokens
    return None


def _parse_day(key: object) -> date | None:
    """Return the date for a canonical YYYY-MM-DD key, or None for anything else."""
    if not isinstance(key, str):
        return None
    try:
        parsed = date.fromisoformat(key)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == key else None


def _oldest_kept(today: str) -> date:
    """The first day kept: RETENTION_DAYS ending with today, inclusive."""
    return date.fromisoformat(today) - timedelta(days=RETENTION_DAYS - 1)


def _valid_bucket(bucket: object) -> bool:
    return (
        isinstance(bucket, dict)
        and set(bucket) == set(BUCKET_FIELDS)
        and all(_is_count(bucket[name]) for name in BUCKET_FIELDS)
    )


def _validate_days(days: object) -> dict[str, Any]:
    """Return the buckets if they have the expected shape; else raise ValueError."""
    if not isinstance(days, dict):
        raise ValueError("token usage has no day buckets")
    for key, roles in days.items():
        if _parse_day(key) is None or not isinstance(roles, dict):
            raise ValueError("token usage has a malformed day")
        for role, tools in roles.items():
            if role not in ROLES or not isinstance(tools, dict):
                raise ValueError("token usage has an unknown role")
            for tool, bucket in tools.items():
                if tool not in TOOLS or not _valid_bucket(bucket):
                    raise ValueError("token usage has a malformed bucket")
    return days


def _read_days(path: Path) -> dict[str, Any]:
    """Return validated day buckets; OSError if missing, ValueError if corrupt."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise ValueError("unrecognised token usage format")
    return _validate_days(value.get("days"))


def _prune(days: dict[str, Any], today: str) -> None:
    """Drop every day outside the retention window."""
    oldest = _oldest_kept(today)
    for key in list(days):
        day = _parse_day(key)
        if day is None or day < oldest:
            del days[key]


def _add(days: dict[str, Any], record: UsageRecord) -> None:
    """Add one call to its day, role and tool bucket."""
    tools = days.setdefault(record.day, {}).setdefault(record.role, {})
    bucket = tools.setdefault(record.tool, dict.fromkeys(BUCKET_FIELDS, 0))
    bucket["calls"] += 1
    if record.tokens is None:
        bucket["unknown"] += 1
    else:
        bucket["tokens"] += record.tokens


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock across a read-modify-write of the store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(f"{path.name}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def record_usage(record: UsageRecord, path: Path | None = None) -> bool:
    """Count one call in the store. Never raises; returns False if it was not saved.

    Counting must never fail a plan or an implementation run.
    """
    target = Path(path if path is not None else TOKEN_USAGE_PATH)
    if record.tokens is None:
        logger.info(
            "Token usage: %s/%s reported no count; counted as unknown",
            record.role,
            record.tool,
        )
    try:
        with _locked(target):
            try:
                days = _read_days(target)
            except FileNotFoundError:
                days = {}
            except ValueError:
                logger.warning("Token usage store is corrupt; starting it again")
                days = {}
            _add(days, record)
            _prune(days, record.day)
            return write_json_atomic(target, {"format": FORMAT, "days": days})
    except OSError as error:
        logger.warning("Could not record token usage: %s", error)
        return False


def _omit(level: int, reason: str) -> None:
    """Log why the section is omitted, once per distinct reason, not every tick."""
    global _last_omission
    if reason != _last_omission:
        _last_omission = reason
        logger.log(level, "Token usage omitted from the snapshot: %s", reason)


def _rows(day_buckets: dict[str, Any]) -> list[dict]:
    """List one day's buckets in a fixed order, copying only whitelisted fields."""
    rows = []
    for role in ROLES:
        for tool in TOOLS:
            bucket = day_buckets.get(role, {}).get(tool)
            if bucket is not None:
                counts = {name: bucket[name] for name in BUCKET_FIELDS}
                rows.append({"role": role, "tool": tool, **counts})
    return rows


def usage_section(path: Path | None = None, today: str | None = None) -> dict | None:
    """Return the snapshot's token_usage section, or None if the store is unusable.

    Holds only dates, roles, tool names and integer counts. Days outside the
    retention window are left out even if the store has not been pruned yet.
    """
    global _last_omission
    target = Path(path if path is not None else TOKEN_USAGE_PATH)
    try:
        days = _read_days(target)
    except FileNotFoundError:
        _omit(logging.INFO, f"no usage recorded yet at {target}")
        return None
    except (OSError, ValueError) as error:
        _omit(logging.WARNING, f"store unreadable: {error}")
        return None
    _last_omission = None
    oldest = _oldest_kept(today or utc_day())
    kept = [key for key in sorted(days) if _parse_day(key) >= oldest]
    return {
        "window_days": RETENTION_DAYS,
        "days": [{"date": key, "usage": _rows(days[key])} for key in kept],
    }
