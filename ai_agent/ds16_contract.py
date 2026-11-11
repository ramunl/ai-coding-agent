"""DS16 v1 vocabulary and field checks; no live evidence collection."""

import math
import re
from datetime import datetime

VERSION = 1
MAX_BYTES = 262144
CASE_OUTCOMES = {
    "source_heavy_revision": {"sources_used": 3, "revisions_applied": 1},
    "failing_build_repair": {"initial_build_exit_code": 1, "repairs_applied": 1},
    "screenshot_delivery": {"supported": True, "delivered": True},
    "cancellation": {"cancel_requested": True, "terminal_state": "cancelled"},
    "provider_outage": {"outage_observed": True, "terminal_state": "handled"},
}
MEASUREMENTS = {
    "completion": "ratio",
    "corrections": "count",
    "peak_memory": "MiB",
    "elapsed_time": "s",
    "actual_cost": "USD",
    "estimated_cost": "USD",
}


class Contract:
    """Accumulate bounded, field-oriented errors without echoing input values."""

    def __init__(self, document: dict) -> None:
        """Bind evidence checks to a single run and evidence class."""
        self.document = document
        self.errors: list[str] = []
        self.references: dict[str, str] = {}

    def require(self, valid: bool, path: str, message: str) -> None:
        """Record a failed condition."""
        if not valid and len(self.errors) < 100:
            self.errors.append(f"{path}: {message}")

    def obj(self, value: object, path: str) -> dict:
        """Require a JSON object while permitting further diagnostics."""
        self.require(isinstance(value, dict), path, "required object")
        return value if isinstance(value, dict) else {}

    def text(self, value: object, path: str) -> bool:
        """Require nonempty bounded text."""
        valid = isinstance(value, str) and bool(value.strip()) and len(value) <= 2048
        self.require(valid, path, "required nonempty text (maximum 2048 characters)")
        return valid

    def timestamp(self, value: object, path: str) -> datetime | None:
        """Require an ISO 8601 timestamp with an explicit timezone."""
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
            if parsed is not None and parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
        self.require(False, path, "required ISO 8601 timestamp with timezone")
        return None

    def digest(self, value: object, path: str, size: int = 64) -> bool:
        """Check a full lowercase hexadecimal digest."""
        valid = isinstance(value, str) and bool(
            re.fullmatch(r"[0-9a-f]{" + str(size) + "}", value)
        )
        self.require(valid, path, f"required {size}-character hexadecimal digest")
        return valid

    def reference(self, value: object, path: str, role: str | None = None) -> None:
        """Require a verified reference belonging to this run."""
        self.require(
            isinstance(value, str)
            and value in self.references
            and (role is None or self.references[value] == role),
            path,
            "must reference verified evidence for this run"
            + (f" with role {role}" if role else ""),
        )

    def observation(self, value: object, path: str, expected: object) -> None:
        """Check recorded outcome and its evidence, never a top-level success flag."""
        record = self.obj(value, path)
        actual = record.get("value")
        self.require(
            type(actual) is type(expected) and actual == expected,
            path + ".value",
            f"required outcome {expected!r}",
        )
        self.reference(record.get("evidence_ref"), path + ".evidence_ref")

    def measurement(self, value: object, path: str, unit: str) -> float | None:
        """Require finite nonnegative measurements, units, method and provenance."""
        record = self.obj(value, path)
        number = record.get("value")
        valid = type(number) in (int, float)
        try:
            valid = valid and math.isfinite(number) and number >= 0
        except OverflowError:
            valid = False
        self.require(valid, path + ".value", "required finite nonnegative number")
        self.require(record.get("unit") == unit, path + ".unit", f"required {unit}")
        self.text(record.get("source"), path + ".source")
        self.reference(record.get("evidence_ref"), path + ".evidence_ref")
        return number if valid else None

    def evidence(self, start: datetime | None, end: datetime | None) -> None:
        """Validate indexed evidence identity, integrity metadata and verification."""
        records = self.document.get("evidence")
        self.require(
            isinstance(records, list) and 0 < len(records) <= 128,
            "evidence",
            "required 1..128 evidence records",
        )
        if not isinstance(records, list):
            return
        seen = set()
        for index, item in enumerate(records[:128]):
            path = f"evidence[{index}]"
            before = len(self.errors)
            record = self.obj(item, path)
            identifier = record.get("id")
            if self.text(identifier, path + ".id"):
                self.require(identifier not in seen, path + ".id", "duplicate id")
                seen.add(identifier)
            for key in ("run_id", "commit", "evidence_kind"):
                self.require(
                    record.get(key) == self.document.get(key),
                    path + "." + key,
                    "must match run identity",
                )
            self.require(
                record.get("status") == "verified",
                path + ".status",
                "required verified; pending evidence cannot pass",
            )
            self.text(record.get("uri"), path + ".uri")
            role = record.get("role")
            self.require(
                role in ("observation", "backup", "release", "codex_rollback"),
                path + ".role",
                "required evidence role",
            )
            if self.document.get("evidence_kind") == "production":
                uri = record.get("uri")
                self.require(
                    isinstance(uri, str) and not uri.startswith("fixture:"),
                    path + ".uri",
                    "fixture references are not production evidence",
                )
            self.digest(record.get("sha256"), path + ".sha256")
            captured = self.timestamp(record.get("captured_at"), path + ".captured_at")
            self.require(
                bool(start and end and captured and start <= captured <= end),
                path + ".captured_at",
                "must fall within run timestamps",
            )
            if before == len(self.errors):
                self.references[identifier] = role
