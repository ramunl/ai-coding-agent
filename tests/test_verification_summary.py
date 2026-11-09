"""The verification summarizer is bounded, deterministic and secret-free."""

import hashlib

from ai_agent.usage_details import UsageDetail
from ai_agent.verification_harness import compare_usage, measure_fixture
from ai_agent.verification_summary import (
    file_excerpts,
    summarize_verification,
    verification_artifact_dir,
)


def test_summary_is_deterministic(tmp_path):
    output = "line one\nline two\nline three\n"
    first = summarize_verification(
        output, identity="ci", exit_code=1, artifact_dir=tmp_path
    )
    second = summarize_verification(
        output, identity="ci", exit_code=1, artifact_dir=tmp_path
    )

    assert first.text() == second.text()
    assert first.artifact_sha256 == second.artifact_sha256


def test_head_tail_truncation_elides_middle():
    output = "\n".join(f"line-{index}" for index in range(100))

    summary = summarize_verification(
        output, identity="ci", exit_code=0, head_lines=3, tail_lines=2
    )

    assert summary.line_count == 100
    assert summary.truncated is True
    assert summary.head == ("line-0", "line-1", "line-2")
    assert summary.tail == ("line-98", "line-99")
    assert "line-50" not in summary.text()


def test_exit_code_and_counts_render():
    summary = summarize_verification("a\nb\n", identity="baseline", exit_code=2)

    text = summary.text()
    assert "baseline" in text
    assert "failed (2)" in text
    assert "lines: 2" in text


def test_char_budget_truncates_with_explicit_marker():
    output = "\n".join("x" * 500 for _ in range(20))

    summary = summarize_verification(output, identity="ci", exit_code=1, max_chars=400)

    assert summary.truncated is True
    assert "summary truncated to char budget" in summary.text()
    assert len(summary.text()) < 800


def test_redaction_applies_to_summary_and_artifact(tmp_path):
    output = "ok line\npassword=hunter2\napi_key=abcdef\n"

    summary = summarize_verification(
        output, identity="ci", exit_code=1, artifact_dir=tmp_path
    )

    text = summary.text()
    assert "hunter2" not in text
    assert "abcdef" not in text
    assert "[redacted]" in text

    artifact = tmp_path / (hashlib.sha256("ci".encode()).hexdigest()[:16] + ".log")
    stored = artifact.read_text(encoding="utf-8")
    assert "hunter2" not in stored
    assert "[redacted]" in stored
    assert summary.artifact_sha256 == hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_no_artifact_dir_keeps_summary_in_memory():
    summary = summarize_verification("hello\n", identity="x", exit_code=0)

    assert summary.artifact_path is None
    assert summary.artifact_sha256 is None


def test_file_excerpts_reuse_bounded_range_reads(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "one = 1\ntwo = 2\npassword=leakme\nfour = 4\n", encoding="utf-8"
    )

    excerpt = file_excerpts([("app.py", 2, 3)], repo_path=repo)

    assert "--- app.py ---" in excerpt
    assert "app.py:2: two = 2" in excerpt
    assert "app.py:3: password=[redacted]" in excerpt
    assert "one = 1" not in excerpt
    assert "four = 4" not in excerpt
    assert "leakme" not in excerpt


def test_verification_artifact_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VERIFICATION_ARTIFACT_DIR", str(tmp_path / "art"))

    assert verification_artifact_dir() == tmp_path / "art"


def test_verification_artifact_dir_defaults_private(monkeypatch):
    monkeypatch.delenv("VERIFICATION_ARTIFACT_DIR", raising=False)

    path = verification_artifact_dir()

    assert path.name == "verification"
    assert path.is_absolute()


def _detail(total_tokens, cost, output_chars):
    return UsageDetail(
        role="implementer",
        runner="claude-code",
        provider="claude-code",
        total_tokens=total_tokens,
        cost=cost,
        output_chars=output_chars,
    )


def test_compare_usage_reports_regressions_separately():
    before = [_detail(100, 0.01, 500)]
    after = [_detail(150, 0.03, 900)]

    report = compare_usage(
        before, after, token_regression_pct=5.0, cost_regression_usd=0.01
    )

    assert report["before"]["tokens"] == 100
    assert report["after"]["tokens"] == 150
    assert any("tokens" in entry for entry in report["regressions"])
    assert any("cost" in entry for entry in report["regressions"])
    assert any("output_chars" in entry for entry in report["regressions"])


def test_measure_fixture_tracks_bounded_size_and_kept_markers():
    text = "\n".join(f"line {index}" for index in range(200))

    result = measure_fixture(text, identity="fixture", markers=("line 0", "line 199"))

    assert result["truncated"] is True
    assert result["summary_chars"] < result["input_chars"]
    assert result["markers_kept"]["line 0"] is True
    assert result["markers_kept"]["line 199"] is True
