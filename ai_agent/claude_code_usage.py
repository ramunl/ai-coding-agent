"""Parse sanitized claude-code stream-json usage without double-counting.

The Claude Code CLI in ``--output-format stream-json`` emits one JSON object
per line: an ``init`` system event (the reported model id), per-turn
``assistant`` messages each carrying their own ``usage``/``costUSD``, optional
``stream_event``/``reconnect`` re-emissions of blocks already seen, and a final
``result`` event whose ``usage``/``total_cost_usd`` is the run's cumulative
total. This module reduces that stream to one :class:`ClaudeCodeUsage`:

- The final ``result`` usage is authoritative when present; per-turn blocks are
  not summed on top of it (wrapper total plus per-request usage would
  double-count).
- Without a usable result (cancelled, failed or truncated), the unique per-turn
  blocks are summed, deduplicating re-emitted messages by message id and
  ignoring ``stream_event``/``reconnect`` wrappers that only re-echo them.
- The wrapper runner ("claude-code") is kept separate from the model-request
  provider/model, which derive only from the reported model id — never from the
  tool label or ``CLAUDE_CODE_AUTH``.
- Missing fields stay None (unknown), never zero. Cache and reasoning tokens
  are reported separately and never added into the total.

The module also carries a small, dated list-price snapshot used ONLY for the
separate estimate recorded next to the measured charge; it is never used to
derive a measured cost.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)

# Dated list-price snapshot (USD per million tokens, peak tier). Estimates
# only — never a measured charge. Verify against the vendor's current price
# page before relying on the estimate; a model absent from this table yields
# estimate_missing_rate=True.
_PRICE_LIST_DATE = "2026-11-09"
PRICE_LIST_SOURCE = f"deepseek-list-price-{_PRICE_LIST_DATE}"
_PRICES = {
    "deepseek-v4-pro": {"input": 0.28, "output": 0.84},
    "deepseek-v4-flash": {"input": 0.14, "output": 0.42},
}


def claude_code_stream_args() -> list[str]:
    """Stream-json flags shared by the planner, implementer and image path."""
    return [
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]


def _events(text: str) -> list[dict]:
    """JSON objects found on their own lines; everything else is ignored."""
    events = []
    for line in (text or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_cost(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _count(value: object) -> int | None:
    return value if _is_count(value) else None


def _cost(value: object) -> float | None:
    return float(value) if _is_cost(value) else None


def _model_id(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(frozen=True)
class ClaudeCodeUsage:
    """Deduplicated usage facts from one claude-code stream-json run."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    provider: str | None = None
    complete: bool = False
    cancelled: bool = False
    is_error: bool = False

    def tokens(self) -> int | None:
        """The cache-safe total for the legacy aggregate store."""
        return self.total_tokens


def provider_for_model(model: str | None) -> str | None:
    """The model-request provider implied by a reported model id, or None.

    Only a reported model id is evidence. The tool label and
    ``CLAUDE_CODE_AUTH`` are never consulted, and an unrecognised id stays None
    so the caller records the neutral wrapper provider instead of guessing.
    """
    if not model:
        return None
    lowered = model.strip().lower()
    if lowered.startswith("deepseek"):
        return "deepseek"
    if lowered.startswith("claude"):
        return "claude"
    return None


def _usage_fields(usage: dict) -> dict:
    """Anthropic-shaped usage fields, kept separate and never totaled here."""
    return {
        "input_tokens": _count(usage.get("input_tokens")),
        "output_tokens": _count(usage.get("output_tokens")),
        "cache_read_tokens": _count(usage.get("cache_read_input_tokens")),
        "cache_creation_tokens": _count(usage.get("cache_creation_input_tokens")),
        "reasoning_tokens": _count(usage.get("reasoning_tokens")),
    }


def _usable(usage: dict) -> bool:
    """A usage block counts only when both input and output are real counts."""
    return (
        _count(usage.get("input_tokens")) is not None
        and _count(usage.get("output_tokens")) is not None
    )


def _total(fields: dict) -> int | None:
    if fields["input_tokens"] is not None and fields["output_tokens"] is not None:
        return fields["input_tokens"] + fields["output_tokens"]
    return None


def _usage_block(event: dict) -> tuple[dict, str | None, float | None] | None:
    """A per-request usage dict, its dedupe id and its cost, or None.

    ``stream_event`` wrappers re-echo the assistant message they carry, so they
    share the same message id and are deduped the same way; ``reconnect``
    markers carry no usage and are skipped entirely.
    """
    message = None
    if event.get("type") == "assistant":
        message = event.get("message")
    elif event.get("type") == "stream_event":
        inner = event.get("event")
        if isinstance(inner, dict):
            message = inner.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    identity = message.get("id")
    identity = identity if isinstance(identity, str) and identity else None
    return usage, identity, _cost(message.get("costUSD"))


def _result_usage(event: dict) -> dict | None:
    usage = event.get("usage")
    return usage if isinstance(usage, dict) else None


def _result_cost(event: dict) -> float | None:
    value = event.get("total_cost_usd")
    if value is None:
        value = event.get("costUSD")
    return _cost(value)


def _sum_blocks(blocks: list[tuple[dict, str | None, float | None]]) -> dict:
    """Sum each field over the counted blocks; a field missing from any block
    stays unknown (None), never zero."""
    fields = {}
    for name in _COUNT_FIELDS:
        values = [_usage_fields(usage)[name] for usage, _id, _cost in blocks]
        if values and all(value is not None for value in values):
            fields[name] = sum(values)
        else:
            fields[name] = None
    return fields


def _sum_costs(blocks: list[tuple[dict, str | None, float | None]]) -> float | None:
    costs = [cost for _usage, _id, cost in blocks]
    if not costs or any(cost is None for cost in costs):
        return None
    return round(sum(costs), 8)


def parse_stream(text: str) -> ClaudeCodeUsage:
    """Reduce one stream-json run to deduplicated, attributed usage facts.

    Never raises: a malformed or empty stream yields a record with every count
    unknown and ``complete=False``.
    """
    model: str | None = None
    result: dict | None = None
    cancelled = False
    blocks: list[tuple[dict, str | None, float | None]] = []
    seen: set[str] = set()

    for event in _events(text):
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            reported = _model_id(event.get("model"))
            if model is None and reported is not None:
                model = reported
        elif etype == "cancel":
            cancelled = True
        elif etype == "result":
            result = event
        if model is None and etype == "assistant":
            message = event.get("message")
            if isinstance(message, dict):
                reported = _model_id(message.get("model"))
                if reported is not None:
                    model = reported
        block = _usage_block(event)
        if block is None:
            continue
        usage, identity, _cost_value = block
        if not _usable(usage):
            continue
        if identity is None or identity not in seen:
            if identity is not None:
                seen.add(identity)
            blocks.append(block)

    is_error = False
    if result is not None:
        is_error = bool(result.get("is_error"))
        cancelled = cancelled or result.get("subtype") == "cancel"
        usage = _result_usage(result)
        if usage is not None and _usable(usage):
            fields = _usage_fields(usage)
            return ClaudeCodeUsage(
                **fields,
                total_tokens=_total(fields),
                cost_usd=_result_cost(result),
                model=model,
                provider=provider_for_model(model),
                complete=(
                    result.get("subtype") == "success" and not result.get("is_error")
                ),
                cancelled=cancelled,
                is_error=is_error,
            )

    fields = _sum_blocks(blocks)
    return ClaudeCodeUsage(
        **fields,
        total_tokens=_total(fields),
        cost_usd=_sum_costs(blocks),
        model=model,
        provider=provider_for_model(model),
        complete=False,
        cancelled=cancelled,
        is_error=is_error,
    )


def result_text(text: str) -> str:
    """The final result event's prose/JSON, or '' when the stream has none."""
    results = [event for event in _events(text) if event.get("type") == "result"]
    if not results:
        return ""
    return str(results[-1].get("result") or "")


def estimate_fields(
    model: str | None, input_tokens: int | None, output_tokens: int | None
) -> dict:
    """A dated list-price estimate, kept separate from any measured charge.

    Returns the estimate fields for a :class:`UsageDetail`. The estimate is
    input plus output tokens at the snapshot's peak rate, in USD, with the
    snapshot source and rate tier recorded. When the model has no rate in the
    snapshot — or the token counts are unknown — ``estimate_usd`` stays None and
    ``estimate_missing_rate`` records the rate gap so the estimate is never
    invented.
    """
    entry = _PRICES.get((model or "").strip().lower()) if model else None
    if entry is None:
        return {
            "estimate_usd": None,
            "estimate_currency": None,
            "estimate_source": None,
            "estimate_rate": None,
            "estimate_missing_rate": True,
        }
    if not _is_count(input_tokens) or not _is_count(output_tokens):
        return {
            "estimate_usd": None,
            "estimate_currency": "USD",
            "estimate_source": PRICE_LIST_SOURCE,
            "estimate_rate": "peak",
            "estimate_missing_rate": False,
        }
    amount = (
        input_tokens * entry["input"] + output_tokens * entry["output"]
    ) / 1_000_000
    return {
        "estimate_usd": round(amount, 6),
        "estimate_currency": "USD",
        "estimate_source": PRICE_LIST_SOURCE,
        "estimate_rate": "peak",
        "estimate_missing_rate": False,
    }
