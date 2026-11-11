# DS16 evidence contract v1

This milestone defines an offline evidence contract and rejection gate. Production
validation remains **pending Ops execution and acceptance**. The checked-in data
is synthetic: it proves contract enforcement, not benchmark performance,
deployment safety, or that a rollback ran. Cached service health and tool-version
checks do not fill any missing benchmark or rollback evidence.

Run from the repository root:

```sh
python -m ai_agent.ds16_validation tests/fixtures/ds16/valid.json
python -m ai_agent.ds16_validation /path/to/ops-evidence.json
scripts/baseline.sh
```

The CLI reads one regular JSON file, at most 262144 bytes, with a maximum nesting
depth of 20. Symlinks, special files, duplicate JSON keys, invalid JSON and
non-finite JSON constants are rejected. It prints JSON and exits 0 for a contract
pass, 1 for rejection (argument errors exit 2). Errors identify fields; input
values and file contents are not printed. Diagnostics are capped at 100 errors.
There is no network access, evidence fetching, subprocess execution, configuration
import, deployment, or service mutation in the validator.

## Envelope and evidence

`ai_agent/ds16_contract.py` defines version 1. `valid.json` in
`tests/fixtures/ds16/` is a complete executable example. Required fields:

| Field | Requirement |
| --- | --- |
| `schema_version` | Integer `1` |
| `run_id`, `repository` | Nonempty run identifier and repository identity |
| `commit` | Full 40-character lowercase hexadecimal commit |
| `evidence_kind` | `fixture` or `production` |
| `started_at`, `finished_at` | Ordered ISO 8601 timestamps with timezones |
| `provider_route` | Nonempty `provider`, `model`, `route`; separate `native_anthropic_status` (`available`, `unavailable`, `not_tested`) and `native_anthropic_note` |
| `environment` | Nonempty `host`, `os`, `runtime`, `collector` |
| `evidence` | 1–128 evidence records |
| `policy`, `cases`, `rollback` | Required structures described below |

Each evidence record has a unique `id`, matching `run_id`, `commit` and
`evidence_kind`, `status: verified`, `uri`, a 64-character lowercase `sha256`,
`captured_at` within the run interval, and `role` (`observation`, `backup`,
`release`, `codex_rollback`). Text fields are bounded to 2048 characters.
Production evidence cannot reference `fixture:` URIs. Pending, absent, mixed-kind
or foreign-run evidence cannot pass. The commit identifies the benchmark run;
the rollback release digest separately identifies the restored version.

References are local IDs into this evidence list. The offline gate checks recorded
metadata and relationships, not artifact contents, signatures, authenticity or
remote availability. Ops must verify the actual artifacts and their digests,
identity, timestamps and provenance before accepting production evidence. Synthetic
digests and URIs in fixtures are deliberately not real artifact claims. Relabeling
a fixture is not a production run.

## Measurements and one-worker policy

`policy.worker_count` is an observation with `value: 1` and `evidence_ref`.
`policy.memory_limit` is an explicit positive measurement in `MiB`.
`policy.memory_scope` and `policy.measurement_methodology` must describe the scope
and methodology. No production memory threshold is supplied or inferred.
The fixture's 256 MiB limit is arbitrary test data, not an acceptable VPS budget.
Ops must record the agreed limit and measurement policy before collecting a run:
include child processes, sampling/peak accounting, worker concurrency, timing,
completion criteria, correction accounting and actual/estimated cost accounting.

Every case requires all six measurements. Each is an object containing `value`,
`unit`, a nonempty `source` explaining the measurement method/source, and a
verified `evidence_ref`:

| Measurement | Unit | Gate |
| --- | --- | --- |
| `completion` | `ratio` | Exactly 1: scenario completed, including cancellation/outage handling |
| `corrections` | `count` | Nonnegative integer intervention count |
| `peak_memory` | `MiB` | Nonnegative, no greater than recorded policy limit |
| `elapsed_time` | `s` | Nonnegative measured wall time |
| `actual_cost` | `USD` | Nonnegative actual accounted cost |
| `estimated_cost` | `USD` | Nonnegative estimate with its own source |

All numbers must be finite; booleans, numeric strings, absent values and negatives
are invalid. Zero is permitted when explicitly measured and sourced, not as an
unknown-value placeholder. No cost, duration or corrections performance threshold
is invented by this milestone.

## Five required scenarios

`cases` contains exactly one entry for each name below. Each has `case`,
`measurements`, and `outcomes`. Outcomes are observations of the form
`{"value": ..., "evidence_ref": "..."}`. Boolean, integer and string outcomes
are checked with their exact types. Version 1 uses the following controlled
scenario outcomes:

| Case | Required case-specific outcomes |
| --- | --- |
| `source_heavy_revision` | `sources_used: 3`, `revisions_applied: 1` |
| `failing_build_repair` | `initial_build_exit_code: 1`, `repairs_applied: 1` |
| `screenshot_delivery` | `supported: true`, `delivered: true` |
| `cancellation` | `cancel_requested: true`, `terminal_state: cancelled` |
| `provider_outage` | `outage_observed: true`, `terminal_state: handled` |

Use three distinct source artifacts and one requested revision for the first
scenario; record source and revision artifacts. The repair scenario must record
the initial failing command and repaired build. Screenshot evidence must show a
supported delivery path and actual receipt, not merely an image generated on disk.
Cancellation evidence must show terminal cancellation and cleanup. Outage evidence
must show the actual route failure and the observed handling through existing
fallback or explicit failure reporting; `handled` does not claim native route
availability. Preserve transcripts and provider route details in evidence.

Every case additionally requires `build_exit_code: 0`, `orphan_workers: 0`,
`lost_history_entries: 0`, and `worker_count: 1`, each backed by evidence. For
cancellation/outage, the build probe checks the resulting repository state; case
completion does not claim the interrupted implementation finished. Record process
inventory and history reconciliation, including before/after identities. A
standalone `success` or `status` flag cannot override any of these checks.

## Codex rollback

`rollback` requires the matching `run_id`, `target: codex`, `status: verified`,
and `completed_at` within the run before the post-rollback probes. Required
`backup_ref`, `release_ref`, `execution_ref` reference evidence with roles
`backup`, `release`, `codex_rollback`, respectively. A healthy deployment record
cannot substitute for rollback execution. In particular,
`deployment_evidence.py` intentionally rejects rolled-back deployments; its
healthy-deploy result is not proof of a successful Codex rollback.

Each of `rollback.state`, `rollback.config`, and `rollback.release` contains
`expected_sha256`, `restored_sha256`, and `evidence_ref`. Both digests must be
valid and equal. Ops must capture the expected backup/config/release artifacts
before rollback and independently verify the restored versions afterward.
`post_rollback_probes` requires observations `codex_route: codex`,
`build_exit_code: 0`, `orphan_workers: 0`, `lost_history_entries: 0`. Their evidence
must be captured strictly after `completed_at`. Pending execution or probes
cannot pass.

## Gate results, fixtures and Ops handoff

The output status is derived as `rejected`, `fixture_contract_pass`, or
`production_contract_pass`; `production_acceptance` is always `pending_ops`.
Additional fields are ignored for forward-compatible annotations and have no
acceptance authority. An offline production contract pass means the submitted
record is internally conformant, not that production has been accepted.

The `*.mutation.json` fixtures specify a path, replacement value and expected
error field against `valid.json`. Tests apply them and assert rejection even with
false success flags. They cover incomplete/duplicate cases, missing measurements
and policy limits, exceeded memory, pending/foreign evidence, restored-state
mismatch, early probes, and unverified rollback. Additional parameterized tests
cover invalid measurements, required fields/outcomes, malformed inputs, bounded
reading and CLI exit behavior. `tests/test_ds16_validation.py` is collected by the
existing CI `pytest` command; no separate opt-in test job is needed.

Ops handoff remains open: agree and record the production measurement policy and
one-worker memory budget; collect all five cases under the actual recorded route,
commit and environment; retain backup and release artifacts; execute a controlled
Codex rollback through Ops controls; capture restored state/config/release checks
and post-rollback probes; validate the exported JSON offline and review the actual
artifacts. No live execution has been performed by this milestone.

DS06/07/09/11/12/15 readiness is outside this milestone. OpenCode and DS13/14
remain conditional. Unavailable native Anthropic routes must be recorded
separately, not represented as successful coverage. This change does not deploy,
restart workers, alter unlimited-runtime or Stop controls, change PM/Ops
independence, or modify existing provider fallback routes.
