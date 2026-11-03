# Context status v1

`ai_agent.context_status` defines an additive, display-only contract for later
initiative/task publication. It never authorizes execution or replaces CTX03
approval bindings or CTX04 dependency verification. No snapshot publication,
archive migration, owner-management controls, or live deployment is included.

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
Invalid timestamps become null. Unknown enum or blocker values are rejected
with fixed errors that do not echo inputs.

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

Successful packet assembly captures integer `observed_at`, `verified`, and
omission/truncation counts in `ContextPacket.metadata`. Omission counts count
omitted optional records; truncation counts count envelope truncation events
(current assembly omits whole records, so this is observed zero). This is not a
count of all possible repository context or redactions. Skipping dependency
verification marks the observation unverified. Failed assembly emits no success
metadata; it retains existing fail-closed behavior.

Receipt creation stores the bounded versioned projection under
`packet_metadata`, next to the existing internal `packet`. The sidecar is not
inserted into packet text, packet ID/digest, reference records, plan approval
bindings or run bindings. Packet equality remains based on content/reference,
not observation time. No approval is refreshed or invalidated by observing time.

Old packets can still be constructed without metadata. Missing sidecars,
unsupported versions and malformed fields remain unknown with nullable counts
and timestamps; never derive freshness from load time or infer zero omissions
from absence. `PacketMetadata.from_dict` reads sidecars conservatively. Current
plan/archive consumers may discard the additive receipt metadata; durable
initiative/task publication and historical migration are later milestones.
This milestone tests the contract and capture boundary only, not end-to-end
snapshot compatibility or comprehensive publication redaction.
