"""Durable effect journal and write-once handoffs, independent of Telegram."""

import json
import time
from contextvars import ContextVar
from dataclasses import asdict, replace
from threading import RLock
from uuid import uuid4

from ai_agent.context_lifecycle import digest, request_from_spec
from ai_agent.context_models import AttemptSnapshot, EvidenceReference
from ai_agent.context_packets import build_packet
from ai_agent.context_sources import ContextBlockedError
from ai_agent.context_store import (
    ContextPersistenceError,
    ContextStore,
    atomic_write,
    read_bytes,
)
from ai_agent.context_validation import _redact_text, decode_record, sanitized_record

execution_control: ContextVar[dict | None] = ContextVar(
    "execution_control", default=None
)

current_attempt: ContextVar["Attempt | None"] = ContextVar(
    "context_attempt", default=None
)


def _redact(value: object) -> object:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class Attempt:
    """One execution; persist intent before effects and observations afterwards.

    A journal is per attempt. Execution serialization owns its single writer;
    terminal archive writes additionally use CTX01's per-record lock.
    """

    def __init__(
        self, spec: dict, branch: str, data: dict, *, recovering: bool = False
    ) -> None:
        """Bind an attempt to its exact scope and a fresh execution identity."""
        self.lock = RLock()
        self.request = request_from_spec(spec)
        self.store = next(
            s for s in self.request.stores if s.scope == self.request.scope
        )
        self.data = data
        self.state = {
            "spec": spec,
            "branch": branch,
            "events": {},
            "latest": {},
            "schema_version": 1,
            "terminal": None,
            "attempt_id": uuid4().hex,
            "started_at": time.time(),
        }
        self.name = f"lifecycle-{self.state['attempt_id']}.json"
        self.request = replace(self.request, attempt_id=self.state["attempt_id"])
        self.packets = [] if recovering else [build_packet(self.request).reference]
        self.failed = False

    def save(self) -> None:
        """Fail closed before effects; keep recovery visible if a write fails."""
        try:
            with self.store.directory() as directory:
                # Specs may contain legacy reopen metadata; redact before any
                # durable write as well as at prompt/snapshot boundaries.
                payload = json.dumps(_redact(self.state)).encode()
                if len(payload) > 512 * 1024:
                    raise ContextBlockedError("Attempt journal limit exceeded")
                atomic_write(directory, self.name, payload)
            self.failed = False
        except Exception:
            self.failed = True
            self.data["context_recovery_pending"] = True
            raise ContextBlockedError("Attempt journal unavailable; recovery required")

    def checkpoint(self, stage: str, facts: dict, *, required: bool = False) -> None:
        """Idempotent observations; never store prompts, logs or model test claims."""

        safe = _redact(facts)
        with self.lock:
            self._checkpoint(stage, safe, required=required)

    def _checkpoint(self, stage: str, facts: dict, *, required: bool) -> None:
        key = digest([stage, facts])
        event = {
            "stage": stage,
            "facts": facts,
            "provenance": "application",
            "observed_at": time.time(),
        }
        if self.state["terminal"]:
            raise ContextBlockedError("Terminal attempt is immutable")
        latest = self.state.setdefault("latest", {})
        if key in self.state["events"] and latest.get(stage) == key and not self.failed:
            return
        latest[stage] = key
        self.state["events"][key] = event
        try:
            self.save()
        except ContextBlockedError:
            if required:
                raise

    def intent(self, effect: str) -> None:
        """A failed observation write prevents the next external effect."""
        if self.failed:
            raise ContextBlockedError("Handoff recovery required before another effect")
        self.checkpoint(effect + "_intent", {}, required=True)

    def finish(self, outcome: str) -> None:
        """Persist immutable terminal result before marking the journal complete."""
        if self.state["terminal"]:
            if self.state["terminal"] != outcome:
                raise ContextBlockedError("Terminal attempt is immutable")
            return
        facts = [entry for entry in self.state["events"].values()]
        evidence = tuple(
            EvidenceReference(
                evidence_id=digest(entry),
                source={"pr": "pull_request", "ci": "test"}.get(
                    entry["stage"], "commit"
                ),
                locator=str(entry["facts"].get("url") or entry["facts"].get("sha")),
                summary=f"Application observed {entry['stage']}: "
                + str(entry["facts"].get("state", "")),
            )
            for entry in facts
            if entry["stage"] in {"pr", "commit", "push", "ci"}
        )
        evidence += tuple(
            EvidenceReference(
                evidence_id=digest(entry),
                source="test",
                locator="verification:v1",
                summary=json.dumps(
                    entry["facts"], sort_keys=True, separators=(",", ":")
                ),
            )
            for entry in facts
            if entry["stage"] == "verification"
        )
        latest = self.state.get("latest", {})
        observed = {
            stage: self.state["events"][key]["facts"]
            for stage, key in latest.items()
            if key in self.state["events"]
        }
        checkout = observed.get("checkout", {})
        changed = observed.get("diff", {})
        paths = changed.get("files", [])
        summary = (
            f"Execution {outcome}; model-reported tests unverified. "
            f"Branch: {self.state['branch']}; checkout HEAD: "
            f"{checkout.get('sha', 'unknown')}; final commit: "
            f"{observed.get('commit', {}).get('sha', 'unknown')}. "
            f"Changed paths ({len(paths)}): "
            + ", ".join(str(path)[:120] for path in paths[:12])
            + ("; additional paths omitted" if len(paths) > 12 else "")
            + f". Diff digest: {changed.get('sha256', 'unknown')}. "
            + f"Last observed phase: {facts[-1]['stage'] if facts else 'start'}. "
            + (
                "Remaining work: inspect saved changes and original requirements; "
                "completion and local build results are unknown."
                if outcome != "succeeded"
                else "Verification checks observed by application; no PR required."
                if "verification" in observed
                else "CI observed by application; local build claims unverified."
            )
        )
        record = AttemptSnapshot(
            record_id=self.state["attempt_id"],
            scope=self.request.scope,
            task_id=self.request.task_id,
            attempt_id=self.state["attempt_id"],
            outcome=outcome,
            dependencies=self.request.dependencies,
            dependency_ids=self.request.dependency_ids,
            summary=summary,
            evidence=evidence,
            packets=tuple(self.packets),
        )
        try:
            packet_data = [
                event["facts"]["packet"]
                for event in facts
                if event["stage"] == "checkout" and "packet" in event["facts"]
            ]
            approved_packet = (self.state.get("approval") or {}).get("packet")
            if approved_packet:
                packet_data.insert(0, approved_packet)
            if packet_data:
                scoped_packets = [
                    {
                        **packet,
                        "records": [
                            ref
                            for ref in packet.get("records", [])
                            if ref["scope"] == asdict(record.scope)
                        ],
                    }
                    for packet in packet_data
                ]
                record = decode_record({**asdict(record), "packets": scoped_packets})
            # CTX01 pins references within the attempt's repository scope. Full
            # cross-scope packet references remain in the durable journal.
            record = replace(
                record,
                packets=tuple(
                    replace(
                        packet,
                        records=tuple(
                            ref for ref in packet.records if ref.scope == record.scope
                        ),
                    )
                    for packet in record.packets
                ),
            )
            if self.state.get("handoff"):
                record = decode_record(self.state["handoff"])
                if record.outcome != outcome:
                    raise ContextBlockedError("Terminal handoff intent is immutable")
            else:
                record = sanitized_record(record)
                self.state["handoff"] = asdict(record)
                self.save()
            try:
                self.store.write(record, expected_revision=0)
            except ContextPersistenceError:
                # A write can have committed before fsync reported failure.
                existing = self.store.get("attempt", record.record_id)
                if existing != replace(record, revision=1):
                    raise ContextBlockedError("Conflicting terminal handoff")
            reference = {
                "kind": "attempt",
                "record_id": record.record_id,
                "revision": 1,
                "scope": asdict(record.scope),
            }
            for task in self.data.get("tasks", []):
                if (
                    task.get("id") == self.request.task_id
                    and task.get("repo") == self.request.scope.repository.project_name
                    and task.get("branch") == self.state["branch"]
                ):
                    task["context_handoff"] = reference
            self.state["terminal"] = record.outcome
            try:
                self.save()
            except Exception:
                self.state["terminal"] = None
                raise
        except Exception:
            self.failed = True
            self.data["context_recovery_pending"] = True
            # Successful Git/PR results remain usable even if archiving failed.


def checkpoint(stage: str, **facts: object) -> None:
    """Record only facts returned by application Git/GitHub calls."""
    attempt = current_attempt.get()
    if attempt:
        attempt.checkpoint(stage, facts)


def intent(effect: str) -> None:
    """Require a durable intent before a non-atomic external operation."""
    attempt = current_attempt.get()
    if attempt:
        attempt.intent(effect)


def unfinished(store: object) -> list[dict]:
    """Scan authoritative journals, not the optional archive index."""
    import os

    result = []
    with store.directory() as directory:
        names = os.listdir(directory)
        if len(names) > 100_000:
            raise ContextBlockedError("Lifecycle scan limit exceeded")
        for name in names:
            if name.startswith("lifecycle-") and name.endswith(".json"):
                state = json.loads(read_bytes(directory, name, 512 * 1024))
                if (
                    state.get("schema_version") != 1
                    or not isinstance(state.get("events"), dict)
                    or state.get("terminal")
                    not in {None, "succeeded", "failed", "blocked", "cancelled"}
                    or state["spec"]["scope"]["owner_id"] != store.scope.owner_id
                ):
                    raise ContextBlockedError(
                        "Invalid lifecycle journal; recovery required"
                    )
                if state.get("terminal"):
                    draft = decode_record(state["handoff"])
                    if (
                        asdict(draft.scope) != state["spec"]["scope"]
                        or draft.scope.owner_id != store.scope.owner_id
                        or draft.record_id != state["attempt_id"]
                        or draft.attempt_id != state["attempt_id"]
                    ):
                        raise ContextBlockedError("Lifecycle handoff identity mismatch")
                    archive = ContextStore(draft.scope, store.root)
                    try:
                        saved = archive.get("attempt", draft.record_id, 1)
                    except ContextPersistenceError:
                        state["terminal"] = None
                    else:
                        if saved != replace(draft, revision=1):
                            raise ContextBlockedError("Conflicting terminal archive")
                if not state.get("terminal"):
                    result.append({**state, "journal": name})
    return result
