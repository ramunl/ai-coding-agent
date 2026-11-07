# Context status v1

`ai_agent.context_status` defines an additive, display-only contract for later
initiative/task publication. It never authorizes execution or replaces CTX03
approval bindings or CTX04 dependency verification. Snapshot publication is
additive; archive migration, owner-management controls and live deployment
are not included.

All fields are present, including nulls:

| Field | v1 value and bound |
| --- | --- |
| `schema_version` | Integer `1` |
| `health` | `healthy`, `degraded`, `blocked`, `unknown`, `unavailable` |
| `packet_refs` | At most 8 unique objects with only `packet_id` and `sha256`, sorted by ID then digest |
| `freshness` | `fresh`, `stale`, `unknown` |
| `observed_at` | UTC Unix seconds, integer 0–253402300799, or null |
| `omission_count` | Integer 0–65535, or null; larger observed counts saturate |
| `truncation_count` | Same bound and null semantics as omission count |
| `dependency_blockers` | Sorted unique allowlisted codes; at most four |
| `evidence_state` | `not_planned`, `metadata_unavailable`, `stale`, `verified_current`, `unknown` |

Packet IDs accept 1–64 ASCII letters, digits, underscores and hyphens; digests
must be exactly 64 lowercase hexadecimal characters. Invalid refs are excluded
and make derived health unknown. Extra valid refs are deterministically capped
and make health degraded. Duplicate refs are collapsed. These are application
identifiers, never a field for user text. Scoped record refs, owner/repository
identities, paths, prompts, transcripts, free-text reasons and arbitrary metadata
are excluded by explicit projection. Never publish `asdict(PacketReference)` or
an internal receipt: those contain scoped record refs with repository paths.

`ContextStatus.to_json()` uses sorted keys, compact separators and ASCII JSON.
The same inputs and explicit evaluation time yield the same bytes. Boolean,
negative, floating-point and nonnumeric count values become null, not zero.
Invalid timestamps become null. Unknown `health`, `freshness`, `evidence_state`
or blocker values are rejected with fixed errors that do not echo inputs when
writing a status; `ContextStatus.from_dict` instead defaults missing or invalid
`evidence_state` to `unknown` so legacy records never raise on read.

## Versioning and compatibility

The contract is versioned by the integer `schema_version`, currently `1`. v1 is
additive only: a field may be added with a documented null/unknown default, but
existing fields, bounds and meanings never change in place. `PacketMetadata.from_dict`
reads a sidecar only when `schema_version` is exactly `1`; a missing, non-integer
or future `schema_version` yields the legacy default — unknown health/freshness,
null counts and timestamps, empty refs — and never raises or migrates. No
migration runs and historical records are never rewritten: old packets stay
unknown and any future field defaults to null/unknown for them.

The field, bounds, safe-reference and legacy-default tables above are the single
source of truth for the v1 wire shape. `ContextStatus.to_dict` projects exactly
those fields with closed enum value sets, and `to_json` emits sorted, compact
ASCII keys. Code changes must update these tables and the contract pinning test
(`tests/test_context_status_contract.py`) together; management and inbox
contracts are separate and unchanged by this file.

## Evidence semantics and precedence

Unknown means missing, invalid or unverified evidence. Unavailable means an
explicitly observed failed access; missing metadata alone is not failed access.
Health precedence, highest first:

1. Failed context access or `dependency_unavailable`: unavailable.
2. Verified dependency invalidation or pending recovery: blocked.
3. Unknown freshness, missing counts/refs, invalid refs, or
   `dependency_unknown`: unknown.
4. Stale evidence, positive omission/truncation counts, or excess refs: degraded.
5. Otherwise: healthy.

`evidence_state` is an additive field that reports what the projection can say
about retained evidence, independent of health. It is `verified_current` when
freshness is `fresh`, `stale` when freshness is `stale`, `metadata_unavailable`
when a receipt exists but its metadata is missing/unverified, `not_planned` when
the subject is known to have no context identity yet, and `unknown` for legacy or
indeterminate records. The v1 helpers select it from a `planned` flag plus
freshness; it never changes health or freshness. Legacy records predating the
field, and invalid or missing values, default to `unknown` on read and never
raise or migrate.

Freshness evaluates the packet assembly observation, not the age of each source
or a promise of current dependency readiness. It is fresh only with verified
metadata and an age from 0 through 60 seconds inclusive; older evidence is stale.
Missing/unverified observations, future timestamps, and invalid evaluation times
are unknown. Evaluation is pure and requires an explicit `now`; no archive,
network or repository reads occur. Consumers still need fresh admission checks.

`dependency_blockers` accepts only `dependency_unknown`,
`dependency_unavailable`, `dependency_changed`, and
`dependency_recovery_pending`. The mapping helper requires callers to verify
identity and freshness before setting `verified=True`. It maps explicit boolean
`access_failed`, `invalidated`, and `recovery_pending` facts to those codes;
verified `status=satisfied` clears blockers only when no such flags exist.
Every other result maps to `dependency_unknown`. Existing CTX04 failures carry
free-text reasons: they cannot establish finer distinctions. Exception messages,
`reason`, arbitrary `code`, PR text and cached success without verification are
never parsed or published.

## Capture boundary and legacy defaults

Successful packet assembly captures integer `observed_at`, `verified`,
omission/truncation counts, `byte_size` and `build_latency_ms` in
`ContextPacket.metadata`. Omission counts count omitted optional records;
truncation counts count envelope truncation events (current assembly omits
whole records, so this is observed zero). This is not a count of all possible
repository context or redactions. Skipping dependency verification marks the
observation unverified. Failed assembly emits no success metadata; it retains
existing fail-closed behavior.

`byte_size` is the exact UTF-8 byte length of the assembled packet text.
`build_latency_ms` is the monotonic-clock build duration in whole milliseconds.
Both are additive integer observations bounded 0–65535 (larger observed values
saturate at 65535) with a null legacy default, and both are measured only from
successful assembly. They are not inserted into packet text, packet ID/digest,
reference records, plan approval bindings or run bindings. Packet equality and
the packet digest remain based on content/reference, not these observations, so
only the per-attempt sidecar keys change between builds.

Receipt creation stores the bounded versioned projection under
`packet_metadata`, next to the existing internal `packet`; the implementation
checkout checkpoint records the same `packet_metadata` so the durable journal
exposes the metrics too. The sidecar is not inserted into packet text, packet
ID/digest, reference records, plan approval bindings or run bindings. No
approval is refreshed or invalidated by observing time or size.

`packet_metadata` and the matching `context_packet` are retained through
planning and execution: `PlanState` carries both, `set_pending_from_plan` and
the bugfix pending builder copy them into pending implementation data,
`enqueue_pending_implementation` copies them into the queued run, and
`remember_run` keeps them on the task's `run` via `RUN_KEYS`. The snapshot
therefore reflects the planning receipt captured at enqueue, not any run-time
HEAD packet (which is only journaled by the checkpoint and never written back).
Neither field enters `plan_binding` or `run_binding` digests, so approval and
revision bindings stay unchanged.

Old packets can still be constructed without metadata. Missing sidecars,
unsupported versions and malformed fields remain unknown with nullable counts,
`byte_size`, `build_latency_ms` and timestamps; never derive freshness from
load time or infer zero omissions, size or latency from absence.
`PacketMetadata.from_dict` reads sidecars conservatively. Current plan/archive
consumers may discard the additive receipt metadata. Publication therefore
reports unknown when observations are absent; it does not add persistence or
migrate historical records.


## Snapshot publication (milestone 2)

The existing snapshot adds `tasks[].context_status` (the unchanged v1 object)
and `initiatives[]`, sorted by opaque initiative ID. Each initiative row contains
only `id` and `context_status`; duplicate initiative IDs collapse. Existing
snapshot fields and publisher timestamps retain their format and meaning.

`context_projection` performs no archive, repository, network or clock reads.
Its callers supply an explicit integer evaluation time; `snapshot()` captures
one time for all context projections. `updated_at` is never evidence of context
freshness. Publication heartbeat cannot refresh `observed_at`.

Task selection uses the pending plan when its non-null plan ID matches the task;
otherwise it uses the retained `run`, or the task itself if no run is present.
The selected source must have a `context_spec` whose task ID equals the task's
ID, with valid opaque task, initiative and attempt IDs. Invalid identities yield
unknown task status and are omitted from the initiative collection. Selection
never combines one source's packet with another source's metadata or falls back
from a mismatched current plan to an older healthy receipt.

Only the selected source's `context_packet` (or receipt `packet`) and
`packet_metadata` are consumed. Safe references retain only packet ID and digest;
scoped references, paths, summaries, transcripts, arbitrary fields, raw errors
and supplied health enums are excluded. The v1 helpers validate bounds and derive
health. Existing plans have no retained metadata, so even valid packet references
normally have unknown freshness and health. No metadata persistence is added.

Tasks with dependency edges default to `dependency_unknown`. The optional
`dependency_observation` input must contain an exactly matching `context_spec`
and fresh, verified v1 `metadata` before its structured `status`, `access_failed`,
`invalidated`, or `recovery_pending` facts can be used by the v1 mapping helper.
`dependency_evidence.refresh` is the producer: it writes the structured
observation into the task and its retained `run` from real checks, with explicit
false flags only for `status=satisfied` and no synthesized flags otherwise; a
task without a context identity gets no observation. The observation shares the
60-second freshness window, so a long run can report `dependency_unknown`
afterward unless re-checked; publication never refreshes it. Legacy
`dependency_check` reasons, invalidation messages and arbitrary blocker codes
are never interpreted as finer dependency evidence.

Initiative IDs come from identity-matched task sources. These receipts establish
only task/attempt observations, so `initiative_statuses` aggregates retained
identity-bound receipts into conservative coverage: an initiative is `healthy`
with `verified_current` evidence only when every retained receipt is
verified-current, and otherwise reports unknown health with the most conservative
child `evidence_state` (`unknown` < `not_planned` < `metadata_unavailable` <
`stale` < `verified_current`). Empty refs and nullable counts never inherit a
single child's observation.

Tests cover the new projection and its snapshot placement with seeded private
data. Existing task notes, dependency reasons and other legacy snapshot fields
are preserved; broader snapshot sanitization and legacy fixture coverage remain
separate milestones. Context evidence is never copied into those legacy fields.
