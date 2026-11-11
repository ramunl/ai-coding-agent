"""Measured run estimates stay scoped, bounded and honest about missing data."""

from ai_agent.finish_estimate import estimate, remember, samples, timing_key


def test_estimate_uses_recent_same_scope_median_and_reports_overrun():
    data = {}
    for seconds in (600, 700, 800, 900, 1000):
        remember(data, "a" * 64, seconds)
        remember(data, "b" * 64, 99999)
    history = data["run_duration_history"]
    assert estimate(history, "a" * 64, 200) == {
        "status": "estimated",
        "samples": 5,
        "remaining_seconds": 600,
    }
    assert estimate(history, "a" * 64, 800)["status"] == "longer_than_usual"
    assert estimate(history, "c" * 64, 200)["status"] == "insufficient_history"


def test_no_fabricated_eta_from_sparse_or_invalid_history():
    assert estimate(None, None, 100)["status"] == "insufficient_history"
    assert samples([{"key": "a" * 64, "seconds": float("nan")}]) == []
    assert samples([{"key": "a" * 64, "seconds": True}]) == []
    data = {}
    for _ in range(120):
        remember(data, "a" * 64, 100)
    assert len(data["run_duration_history"]) == 100
    assert estimate(data["run_duration_history"], "a" * 64, 50)["samples"] == 20
    assert (
        estimate(data["run_duration_history"], "a" * 64, None)["status"]
        == "insufficient_history"
    )


def test_exact_repository_and_provider_define_comparison_group():
    assert timing_key({"repo_path": "/a"}, "claude") != timing_key(
        {"repo_path": "/b"}, "claude"
    )
    assert timing_key({"repo_path": "/a"}, "claude") != timing_key(
        {"repo_path": "/a"}, "codex"
    )
