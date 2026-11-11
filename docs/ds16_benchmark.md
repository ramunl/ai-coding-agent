# DS16 benchmark: fixtures, evidence gate, staged rollout and Codex rollback

DS16 stages the DeepSeek/registry integration, benchmarks representative tasks,
and verifies a rollback to Codex. This milestone adds the reproducible benchmark
definitions, the per-record evidence gate, and a side-effect-free local CLI. The
actual five real integration runs, the memory/cost measurements, and production
acceptance are **Ops-controlled**; nothing here runs an implementation, contacts
a provider, mutates the repository, or deploys.

- `ai_agent/ds16_benchmark.py` — five required task fixtures and the JSON
  evidence record schema.
- `ai_agent/ds16_evidence.py` — offline validators for corrections, memory,
  time, cost, Stop controls, orphan workers, lost history, and false successes.
- `scripts/ds16_benchmark_cli.py` — emits one side-effect-free fixture record.
- `tests/test_ds16_benchmark.py` — validator rejections and CLI output.

## Local, side-effect-free CLI

Run from the repository root:

```sh
python scripts/ds16_benchmark_cli.py --case source_heavy_revision
python scripts/ds16_benchmark_cli.py --case failing_build_repair
python scripts/ds16_benchmark_cli.py --case screenshot_delivery
python scripts/ds16_benchmark_cli.py --case cancellation
python scripts/ds16_benchmark_cli.py --case provider_outage
```

The CLI prints one JSON record to stdout and exits 0 when the record passes the
offline gate. Flags: `--case` (default `source_heavy_revision`), `--run-id`,
`--commit` (defaults to the current `HEAD`), `--repository`, `--started-at`,
`--finished-at`. The record is synthetic: `side_effect_free` is `true`,
`evidence_kind` is `fixture`, and the `ops_note` states that real runs and
production acceptance are Ops-controlled. It is not performance evidence.

## Five required task fixtures

Each fixture is a controlled scenario with case-specific outcomes and the six
measurements (`completion`, `corrections`, `peak_memory`, `elapsed_time`,
`actual_cost`, `estimated_cost`). Every case also requires the common outcomes
`build_exit_code: 0`, `orphan_workers: 0`, `lost_history_entries: 0`,
`worker_count: 1`, `stop_controls_preserved: true`, and
`unlimited_runtime_preserved: true`.

| Case | Case-specific outcomes |
| --- | --- |
| `source_heavy_revision` | `sources_used: 3`, `revisions_applied: 1` |
| `failing_build_repair` | `initial_build_exit_code: 1`, `repairs_applied: 1` |
| `screenshot_delivery` | `supported: true`, `delivered: true` |
| `cancellation` | `cancel_requested: true`, `terminal_state: cancelled` |
| `provider_outage` | `outage_observed: true`, `terminal_state: handled` |

The same scenarios are described in `docs/ds16-evidence.md`; the fixtures here
add the reproducible task prompts/goals and the per-run evidence record shape.

## Evidence record schema

`ai_agent/ds16_benchmark.EVIDENCE_RECORD_SCHEMA` is a JSON Schema (draft
2020-12) for one fixture record. Required envelope fields: `benchmark_version`
(1), `case`, `evidence_kind` (`fixture`), `side_effect_free` (`true`), `run_id`,
`repository`, `commit` (40 lowercase hex), `started_at`, `finished_at`,
`ops_note`, `outcomes`, `measurements`, and `evidence`.

- `outcomes` maps outcome names to observations `{"value", "evidence_ref"}`.
- `measurements` maps the six measurement names to
  `{"value", "unit", "source", "evidence_ref"}`.
- `evidence` is 1–16 entries with `id`, `status: verified`, `uri`, a 64
  lowercase hex `sha256`, `captured_at` within the run interval, and
  `role: observation`.

The validators enforce the same fields in code; the schema is documentation and
single-sourcing for the record shape, not an additional runtime dependency.

## Validators

`ai_agent/ds16_evidence.validate(record, *, memory_limit=256)` returns
`{"valid": bool, "errors": [...]}`. Individual validators are also importable:

- `validate_corrections` — `corrections` is a nonnegative integer `count`.
- `validate_memory` — `peak_memory` in `MiB` is nonnegative and no greater than
  the recorded limit (default 256 MiB; the fixture value is synthetic, not a
  VPS budget).
- `validate_time` — `elapsed_time` in `s` is a nonnegative finite number.
- `validate_cost` — `actual_cost` and `estimated_cost` in `USD` are nonnegative
  finite numbers.
- `validate_stop` — `stop_controls_preserved` and `unlimited_runtime_preserved`
  are `true` with evidence references.
- `validate_orphan_workers` — `orphan_workers` is `0`.
- `validate_lost_history` — `lost_history_entries` is `0`.
- `validate_false_success` — rejects records carrying a standalone `success` or
  `status` flag; success is derived only from the observations and measurements.

Stop, orphan-worker, lost-history and false-success checks exercise the gate
mechanics offline, but a real integration environment is required to observe
them, so production records must carry **Ops-provided evidence** for those
outcomes.

## Staged rollout (Ops-controlled)

Deploy only through the existing manager and only while the worker is idle: a
deploy restarts the service and kills any run in progress.

1. **Record the measurement policy before collecting.** Agree and record the
   one-worker memory budget, worker concurrency, sampling/peak accounting,
   completion criteria, correction accounting, and actual-vs-estimated cost
   accounting. The fixture's 256 MiB limit is synthetic test data, not a VPS
   budget.
2. **Back up durable state, config, and verified release artifacts.** See the
   rollback section below. Retain the backups until acceptance is signed off.
3. **Verify the offline gate.** Run `scripts/baseline.sh` and emit one fixture
   record per case with the CLI; every record must pass `validate`.
4. **Run one controlled integration case.** Pick `failing_build_repair` first,
   on the recorded route/commit/environment, in a disposable worktree. Record
   outcomes and measurements, and confirm no orphan workers and no lost history.
5. **Run all five cases.** Cover `source_heavy_revision`,
   `failing_build_repair`, `screenshot_delivery` (supported route with actual
   receipt, not just an image on disk), `cancellation` (terminal cancellation
   and cleanup), and `provider_outage` (observed failure handled through
   fallback or explicit reporting). Keep the PM/Ops bots AI-independent, keep
   the Codex fallback and the working Claude Code/DeepSeek route, and do not
   restart a running implementation or undo unlimited-runtime and Stop controls.
6. **Execute the Codex rollback drill.** Follow the rollback section below and
   capture restored state/config/release digests and post-rollback probes.
7. **Accept production evidence.** Export the collected run as the DS16 evidence
   document, validate it offline, and have Ops review the actual artifacts
   (digests, identity, timestamps, provenance) before signing off. A production
   contract pass is still `pending_ops`.

## Verified Codex rollback

Rollback restores the Codex provider route and proves the change is reversible.
Capture the expected digests before the drill, switch the route back, deploy
idle, and verify the restored state.

1. **Back up before the drill:**

   ```bash
   STAMP=$(date -u +%Y%m%dT%H%M%SZ)
   BACKUP_DIR="/var/backups/ai-coding-agent-ds16-$STAMP"
   install -d -m 0700 "$BACKUP_DIR"
   cp -a /var/lib/ai-coding-agent/state.json "$BACKUP_DIR/state.json"
   cp -a /var/lib/ai-coding-agent/context "$BACKUP_DIR/context"
   cp -a /etc/ai-coding-agent/ai-coding-agent.env "$BACKUP_DIR/env"
   sha256sum "$BACKUP_DIR/state.json" "$BACKUP_DIR/env" > "$BACKUP_DIR/digests.before"
   ```

2. **Switch the provider route back to Codex.** In
   `/etc/ai-coding-agent/ai-coding-agent.env`, set:

   ```text
   PLANNING_AGENT=codex
   IMPLEMENTATION_AGENT=codex
   ```

   then deploy idle from the Ops bot with `/ai_update coding`.

3. **Confirm the service is healthy and on Codex:**

   ```bash
   systemctl status ai-coding-agent.service --no-pager
   grep -E '^(PLANNING_AGENT|IMPLEMENTATION_AGENT)=' /etc/ai-coding-agent/ai-coding-agent.env
   ```

4. **Run post-rollback probes and record them:**

   ```bash
   scripts/baseline.sh                       # build probe: tests, lint, format
   pgrep -af 'ai-coding-agent'               # one worker, no orphan children
   sha256sum /var/lib/ai-coding-agent/state.json /etc/ai-coding-agent/ai-coding-agent.env \
     | diff "$BACKUP_DIR/digests.before" -
   diff -r "$BACKUP_DIR/context" /var/lib/ai-coding-agent/context
   ```

   Compare `state.json` and the env file digests against the backup and confirm
   the context archive is unchanged. If any diff shows drift, stop and inspect
   before touching live data; restore only with the service stopped, then start
   it again and re-check.

The DS16 evidence record's `rollback` section requires `target: codex`,
`status: verified`, restored state/config/release digests equal to the expected
backup/release digests, and post-rollback probes captured strictly after
`rollback.completed_at`. A healthy deploy record cannot substitute for an
executed rollback.

## Boundaries

This change does not deploy, restart workers, or mutate live server state, and
it does not alter unlimited-runtime or Stop controls. Production validation
remains pending Ops execution and acceptance. DS06/07/09/11/12/15 readiness,
OpenCode (DS13/14), and native Anthropic route availability remain separate.
