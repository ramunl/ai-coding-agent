"""Versioned per-call provider usage details, alongside the legacy aggregates.

The legacy ``token_usage.py`` store still holds daily role/tool/call counts
(format 1) and is not changed. This module adds a second, versioned store with
one bounded record per provider call: runner and provider attribution, the
model when known, separate input/output/cache/reasoning token fields plus a
cache-safe total, a measured tool-reported charge, and a dated list-price
estimate kept strictly separate from that charge. Missing fields stay unknown
(None), never zero, and a total never double-counts cache or reasoning tokens.
The store writes format 2 and still reads format 1 records unchanged.

The store reuses ``token_usage.locked``, the atomic JSON writer and the same
90-day UTC retention discipline (configurable via
``USAGE_DETAILS_RETENTION_DAYS``) with a hard record cap
(``USAGE_DETAILS_MAX_RECORDS``). Prompts, outputs and identifiers never reach
the published snapshot section; only bounded counts and attribution do.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ai_agent.bot.atomic_file import write_json_atomic
from ai_agent.config import (
    USAGE_DETAILS_MAX_RECORDS,
    USAGE_DETAILS_PATH,
    USAGE_DETAILS_RETENTION_DAYS,
)
from ai_agent.token_usage import locked, utc_day

logger = logging.getLogger(__name__)

FORMAT = 2
LEGACY_FORMAT = 1
ROLES = ("planner", "implementer")
RUNNERS = ("codex", "claude-api", "claude-code", "deepseek-api", "opencode")
PROVIDERS = ("codex", "claude", "claude-code", "deepseek")
MAX_MODEL_LENGTH = 200
MAX_ID_LENGTH = 200
MAX_LABEL_LENGTH = 200
MAX_OUTPUT_CHARS = 1_000_000

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "total_tokens",
)
# The dated list-price estimate fields, kept separate from the measured
# tool-reported ``cost``. ``estimate_rate`` is the peak/off-peak tier used and
# ``estimate_missing_rate`` records a rate gap so no estimate is invented.
ESTIMATE_FIELDS = (
    "estimate_usd",
    "estimate_currency",
    "estimate_source",
    "estimate_rate",
    "estimate_missing_rate",
)
LEGACY_STORED_FIELDS = (
    "role",
    "runner",
    "provider",
    "model",
    "day",
    *TOKEN_FIELDS,
    "cost",
    "duration_seconds",
    "output_chars",
    "attempt_id",
    "call_id",
)
STORED_FIELDS = (*LEGACY_STORED_FIELDS, *ESTIMATE_FIELDS)
# The snapshot omits attempt/call identifiers: it must stay secret-free.
PUBLISHED_FIELDS = tuple(
    name for name in STORED_FIELDS if name not in {"attempt_id", "call_id"}
)
_LEGACY_DEFAULT = {
    "estimate_usd": None,
    "estimate_currency": None,
    "estimate_source": None,
    "estimate_rate": None,
    "estimate_missing_rate": False,
}

_last_omission: str | None = None


def new_call_id() -> str:
    """Return a fresh per-call identity used to dedupe repeated writes."""
    return uuid.uuid4().hex


def current_attempt_id() -> str | None:
    """Return the running attempt's identity, or None outside an attempt."""
    from ai_agent.context_attempts import current_attempt

    attempt = current_attempt.get()
    return attempt.state["attempt_id"] if attempt is not None else None


def bounded_output_chars(text: str | None) -> int | None:
    """Return the output length as a bounded integer, never the text itself."""
    if text is None:
        return None
    return min(len(text), MAX_OUTPUT_CHARS)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_optional_text(value: object, limit: int) -> bool:
    return value is None or (
        isinstance(value, str) and 0 < len(value) <= limit and "\n" not in value
    )


def _is_cost(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


@dataclass(frozen=True)
class UsageDetail:
    """One provider call with runner/provider attribution and cache-safe totals."""

    role: str
    runner: str
    provider: str
    model: str | None = None
    day: str = field(default_factory=utc_day)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    # Measured tool-reported charge only; never a dated price estimate or
    # subscription fee. The dated list-price estimate lives in estimate_*.
    cost: float | None = None
    estimate_usd: float | None = None
    estimate_currency: str | None = None
    estimate_source: str | None = None
    estimate_rate: str | None = None
    estimate_missing_rate: bool = False
    duration_seconds: int | None = None
    output_chars: int | None = None
    attempt_id: str | None = None
    call_id: str | None = None

    def __post_init__(self) -> None:
        """Reject values outside the published vocabulary before storing them."""
        if self.role not in ROLES:
            raise ValueError(f"Unknown usage role: {self.role}")
        if self.runner not in RUNNERS:
            raise ValueError(f"Unknown usage runner: {self.runner}")
        if self.provider not in PROVIDERS:
            raise ValueError(f"Unknown usage provider: {self.provider}")
        if not _is_optional_text(self.model, MAX_MODEL_LENGTH):
            raise ValueError("Usage model must be a short string or None")
        for name in ("attempt_id", "call_id"):
            if not _is_optional_text(getattr(self, name), MAX_ID_LENGTH):
                raise ValueError(f"Usage {name} must be a short string or None")
        date.fromisoformat(self.day)
        for name in TOKEN_FIELDS:
            value = getattr(self, name)
            if value is not None and not _is_count(value):
                raise ValueError(f"Usage {name} must be a non-negative integer or None")
        if not _is_cost(self.cost):
            raise ValueError("Usage cost must be a non-negative number or None")
        if not _is_cost(self.estimate_usd):
            raise ValueError("Usage estimate must be a non-negative number or None")
        for name in ("estimate_currency", "estimate_source"):
            if not _is_optional_text(getattr(self, name), MAX_LABEL_LENGTH):
                raise ValueError(f"Usage {name} must be a short string or None")
        if self.estimate_rate not in (None, "peak", "off_peak"):
            raise ValueError("Usage estimate rate must be peak, off_peak or None")
        if not isinstance(self.estimate_missing_rate, bool):
            raise ValueError("Usage estimate missing-rate flag must be a boolean")
        if self.duration_seconds is not None and not _is_count(self.duration_seconds):
            raise ValueError("Usage duration must be a non-negative integer or None")
        if self.output_chars is not None and not _is_count(self.output_chars):
            raise ValueError(
                "Usage output length must be a non-negative integer or None"
            )
        if (
            self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("Usage total must equal input plus output tokens")


def _identity(record: dict) -> tuple[str, str] | None:
    """The dedupe identity, or None when the call carries no identifiers."""
    attempt_id = record.get("attempt_id")
    call_id = record.get("call_id")
    if attempt_id is None and call_id is None:
        return None
    return (attempt_id or "", call_id or "")


def _to_dict(detail: UsageDetail) -> dict[str, Any]:
    return {name: getattr(detail, name) for name in STORED_FIELDS}


def _valid_detail(record: object, *, legacy: bool = False) -> dict:
    """Validate one stored record and return it with every stored field.

    Format-1 records lack the estimate fields and are upgraded in place, so a
    store written before the estimate extension still parses. Raise ValueError
    on any injected or missing field.
    """
    expected = LEGACY_STORED_FIELDS if legacy else STORED_FIELDS
    if not isinstance(record, dict) or set(record) != set(expected):
        raise ValueError("usage detail has an unexpected shape")
    normalized = dict(record)
    if legacy:
        for name, default in _LEGACY_DEFAULT.items():
            normalized.setdefault(name, default)
    UsageDetail(**normalized)  # validate every value
    return normalized


def _read_records(path: Path) -> list[dict]:
    """Return validated detail records; OSError if missing, ValueError if corrupt."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("unrecognised usage details format")
    if value.get("format") == FORMAT:
        legacy = False
    elif value.get("format") == LEGACY_FORMAT:
        legacy = True
    else:
        raise ValueError("unrecognised usage details format")
    records = value.get("records")
    if not isinstance(records, list):
        raise ValueError("usage details has no records")
    return [_valid_detail(record, legacy=legacy) for record in records]


def _oldest_kept(today: str) -> date:
    return date.fromisoformat(today) - timedelta(days=USAGE_DETAILS_RETENTION_DAYS - 1)


def _prune(records: list[dict], today: str) -> list[dict]:
    """Drop records outside the window and past the hard record cap."""
    oldest = _oldest_kept(today)
    kept = [record for record in records if date.fromisoformat(record["day"]) >= oldest]
    if len(kept) > USAGE_DETAILS_MAX_RECORDS:
        kept = kept[-USAGE_DETAILS_MAX_RECORDS:]
    return kept


def _add(records: list[dict], detail: UsageDetail) -> bool:
    """Append one detail unless the same attempt/call id is already present."""
    record = _to_dict(detail)
    identity = _identity(record)
    if identity is not None and any(
        _identity(existing) == identity for existing in records
    ):
        logger.info("Usage detail already recorded; skipped duplicate %s", identity)
        return False
    records.append(record)
    return True


def record_detail(detail: UsageDetail, path: Path | None = None) -> bool:
    """Append one bounded, deduped detail record. Never raises.

    Recording must never fail a plan or an implementation run, including a run
    the owner cancelled through ``implementation_stop``.
    """
    target = Path(path if path is not None else USAGE_DETAILS_PATH)
    if detail.total_tokens is None:
        logger.info(
            "Usage detail: %s/%s reported no token total; kept as unknown",
            detail.role,
            detail.runner,
        )
    try:
        with locked(target):
            try:
                records = _read_records(target)
            except FileNotFoundError:
                records = []
            except ValueError:
                logger.warning("Usage details store is corrupt; starting it again")
                records = []
            if not _add(records, detail):
                return False
            records = _prune(records, detail.day)
            return write_json_atomic(target, {"format": FORMAT, "records": records})
    except OSError as error:
        logger.warning("Could not record usage detail: %s", error)
        return False


def _omit(level: int, reason: str) -> None:
    """Log why the section is omitted, once per distinct reason, not every tick."""
    global _last_omission
    if reason != _last_omission:
        _last_omission = reason
        logger.log(level, "Usage details omitted from the snapshot: %s", reason)


def detail_section(path: Path | None = None, today: str | None = None) -> dict | None:
    """Return the snapshot's optional usage_details section, or None.

    Secret-free: attempt/call identifiers are left out and no prompt or output
    text is ever stored. Records outside the retention window or past the cap
    are omitted even if the store has not been pruned yet.
    """
    global _last_omission
    target = Path(path if path is not None else USAGE_DETAILS_PATH)
    try:
        records = _read_records(target)
    except FileNotFoundError:
        _omit(logging.INFO, f"no usage details recorded yet at {target}")
        return None
    except (OSError, ValueError) as error:
        _omit(logging.WARNING, f"usage details store unreadable: {error}")
        return None
    _last_omission = None
    oldest = _oldest_kept(today or utc_day())
    kept = [record for record in records if date.fromisoformat(record["day"]) >= oldest]
    kept = kept[-USAGE_DETAILS_MAX_RECORDS:]
    published = [{name: record[name] for name in PUBLISHED_FIELDS} for record in kept]
    return {
        "window_days": USAGE_DETAILS_RETENTION_DAYS,
        "records": published,
    }
