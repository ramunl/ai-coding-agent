#!/usr/bin/env python3
"""Emit one side-effect-free DS16 benchmark fixture evidence record.

Run from the repository root:

    python scripts/ds16_benchmark_cli.py --case source_heavy_revision

The record is synthetic and offline: it never runs an implementation, contacts
a provider, or mutates the repository. Real integration runs and production
acceptance are Ops-controlled.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_agent.ds16_benchmark import CASE_NAMES, build_fixture_record
from ai_agent.ds16_evidence import validate


def _resolve_commit() -> str:
    """Return the current HEAD commit, or a synthetic digest outside a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        value = result.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", value):
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    return "0" * 40


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 timestamp."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=CASE_NAMES,
        default="source_heavy_revision",
        help="which of the five required fixtures to emit (default: %(default)s)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="run identifier (default: ds16-fixture-<case>-<timestamp>)",
    )
    parser.add_argument(
        "--commit",
        default=None,
        help="40-character hexadecimal commit (default: current HEAD)",
    )
    parser.add_argument("--repository", default="ai-coding-agent")
    parser.add_argument(
        "--started-at",
        default=None,
        help="ISO 8601 start timestamp (default: now)",
    )
    parser.add_argument(
        "--finished-at",
        default=None,
        help="ISO 8601 finish timestamp (default: now)",
    )
    return parser


def main(argv: list[str]) -> int:
    """Build, validate and print one fixture evidence record."""
    args = build_parser().parse_args(argv)
    now = _now_iso()
    started_at = args.started_at or now
    finished_at = args.finished_at or now
    run_id = args.run_id or (
        f"ds16-fixture-{args.case}-{int(datetime.now().timestamp())}"
    )
    commit = args.commit or _resolve_commit()
    record = build_fixture_record(
        args.case,
        run_id=run_id,
        commit=commit,
        started_at=started_at,
        finished_at=finished_at,
        repository=args.repository,
    )
    verdict = validate(record)
    print(json.dumps(record, indent=2, sort_keys=True))
    if not verdict["valid"]:
        print(
            json.dumps({"self_check": "rejected", "errors": verdict["errors"]}),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
