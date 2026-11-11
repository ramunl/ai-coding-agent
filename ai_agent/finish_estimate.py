"""Bounded duration samples and explicitly approximate active-run estimates."""

import hashlib
import json
import math
from contextvars import ContextVar
from statistics import median

MIN_SAMPLES = 5
MAX_SAMPLES = 100
current_timing_key: ContextVar[str | None] = ContextVar("timing_key", default=None)


def timing_key(identity: dict, provider: str) -> str:
    """Group durations by exact repository identity and selected provider."""
    return hashlib.sha256(
        json.dumps([identity, provider], sort_keys=True).encode()
    ).hexdigest()


def _duration(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def samples(history: object) -> list[dict]:
    """Accept only bounded, finite samples restored from persistent state."""
    if not isinstance(history, list):
        return []
    return [
        {"key": item["key"], "seconds": item["seconds"]}
        for item in history[-MAX_SAMPLES:]
        if isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and len(item["key"]) == 64
        and _duration(item.get("seconds"))
    ]


def remember(data: dict, key: str, seconds: float) -> None:
    """Keep only measured successful runs, without titles, prompts or paths."""
    if _duration(seconds):
        data["run_duration_history"] = (
            samples(data.get("run_duration_history"))
            + [{"key": key, "seconds": seconds}]
        )[-MAX_SAMPLES:]


def estimate(history: object, key: str | None, elapsed: object) -> dict:
    """Estimate remaining implementation-through-CI time, never a timeout."""
    durations = [s["seconds"] for s in samples(history) if s["key"] == key][-20:]
    result = {"status": "insufficient_history", "samples": len(durations)}
    if len(durations) < MIN_SAMPLES or not _duration(elapsed):
        return result
    typical = median(durations)
    if elapsed >= typical:
        return {**result, "status": "longer_than_usual"}
    return {
        **result,
        "status": "estimated",
        "remaining_seconds": max(1, math.ceil(typical - elapsed)),
    }
