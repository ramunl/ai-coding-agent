"""Before/after measurement for the bounded verification-output change.

The harness reuses ``usage_details`` records (token/cost/output_chars) and the
``verification_summary`` summarizer so the same representative fixtures can be
measured before and after the change, with regressions reported separately.
Numbers must be re-measured on the same fixtures for each comparison; older
observations are never cited as fresh.
"""

from __future__ import annotations

from typing import Iterable

from ai_agent.usage_details import UsageDetail, bounded_output_chars
from ai_agent.verification_summary import summarize_verification


def measure_fixture(
    text: str,
    *,
    identity: str,
    markers: tuple[str, ...] = (),
    head_lines: int | None = None,
    tail_lines: int | None = None,
    max_chars: int | None = None,
) -> dict:
    """Measure one representative fixture through the summarizer.

    Returns the raw input size, the bounded summary size (reusing
    ``usage_details.bounded_output_chars``), the summary counts and whether each
    ``marker`` survived — the completion-quality proxy.
    """
    kwargs: dict = {}
    if head_lines is not None:
        kwargs["head_lines"] = head_lines
    if tail_lines is not None:
        kwargs["tail_lines"] = tail_lines
    if max_chars is not None:
        kwargs["max_chars"] = max_chars
    summary = summarize_verification(text, identity=identity, exit_code=1, **kwargs)
    rendered = summary.text()
    return {
        "identity": identity,
        "input_chars": len(text),
        "summary_chars": bounded_output_chars(rendered),
        "line_count": summary.line_count,
        "truncated": summary.truncated,
        "markers_kept": {marker: marker in rendered for marker in markers},
    }


def usage_totals(details: Iterable[UsageDetail]) -> dict:
    """Aggregate token, cost and output_chars across usage-detail records."""
    calls = 0
    tokens = 0
    cost = 0.0
    output_chars = 0
    for detail in details:
        calls += 1
        if detail.total_tokens is not None:
            tokens += detail.total_tokens
        if detail.cost is not None:
            cost += detail.cost
        if detail.output_chars is not None:
            output_chars += detail.output_chars
    return {
        "calls": calls,
        "tokens": tokens,
        "cost": round(cost, 8),
        "output_chars": output_chars,
    }


def compare_usage(
    before: Iterable[UsageDetail],
    after: Iterable[UsageDetail],
    *,
    token_regression_pct: float = 5.0,
    cost_regression_usd: float = 0.01,
) -> dict:
    """Compare before/after usage; report regressions in a separate list.

    ``regressions`` holds only the deltas that got worse beyond tolerance, so a
    caller can surface them apart from the neutral before/after numbers.
    """
    before_totals = usage_totals(before)
    after_totals = usage_totals(after)
    regressions: list[str] = []

    delta_tokens = after_totals["tokens"] - before_totals["tokens"]
    if before_totals["tokens"] and (
        delta_tokens / before_totals["tokens"] * 100 > token_regression_pct
    ):
        pct = delta_tokens / before_totals["tokens"] * 100
        regressions.append(f"tokens +{delta_tokens} ({pct:.1f}%)")

    delta_cost = after_totals["cost"] - before_totals["cost"]
    if delta_cost > cost_regression_usd:
        regressions.append(f"cost +{delta_cost:.6f} USD")

    delta_chars = after_totals["output_chars"] - before_totals["output_chars"]
    if delta_chars > 0:
        regressions.append(f"output_chars +{delta_chars}")

    return {
        "before": before_totals,
        "after": after_totals,
        "regressions": regressions,
    }


__all__ = ["compare_usage", "measure_fixture", "usage_totals"]
