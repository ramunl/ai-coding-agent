# token_usage.py

Counts provider calls per day, role and tool, so the owner can see how much
planning and implementation consume. The counts are stored locally and
published, secret-free, in the Coding snapshot for the separate
`ai-dashboard` service.

Part 1 (this change) records and publishes the data. Part 2, in `ai-dashboard`,
reads the `token_usage` section. Until then nothing displays it.

A second, versioned store (`ai_agent/usage_details.py`) records one bounded
per-call record alongside the untouched format-1 aggregate store. It is
published as the optional `usage_details` snapshot section.

## What is recorded

Each provider call becomes one `UsageRecord`:

| Field    | Values                                            |
|----------|---------------------------------------------------|
| `role`   | `planner` or `implementer`                        |
| `tool`   | `codex` or `claude`                               |
| `day`    | UTC calendar day, `YYYY-MM-DD`                    |
| `tokens` | integer total, or `None` when the provider gave none |

Nothing else is accepted. `UsageRecord` rejects any role or tool outside those
lists, and any `tokens` value that is not a non-negative integer. A prompt,
an output or an identifier therefore cannot be stored by accident.

## Where each count comes from

| Role / tool          | Source                                                  | Status |
|----------------------|---------------------------------------------------------|--------|
| planner / claude     | Claude Code CLI stream-json `result.usage`, deduped across turns (or the API `usage` when `CLAUDE_PLANNER_VIA=api`) | Pinned by sanitized fixtures; cache and reasoning reported separately |
| planner / codex      | `tokens used` line in `codex exec` output                | **Unverified** against the installed CLI (0.162.1); no fixture here |
| implementer / codex  | same `tokens used` parse, from the implementation run   | **Unverified**, as above |
| implementer / claude | Claude Code CLI stream-json `result.usage`, deduped across turns | Pinned by sanitized fixtures |

"Unverified" means the parser is written to the format Codex is believed to
print, but no test or fixture pins that output. To check it on the server, run
a trivial `codex exec` and confirm the `tokens used` line and its number. If
the line is absent or differs, every Codex call is counted as unknown; nothing
is recorded as zero.

Claude Code's planner and implementer runs ask for `--output-format
stream-json`, so their token, cache, reasoning and cost facts come from the
reported `result.usage`/`total_cost_usd`. The stream is reduced by
`ai_agent/claude_code_usage.py`: the final `result` usage is authoritative
when present, per-turn `assistant` blocks are summed only otherwise, and
`stream_event`/`reconnect` re-emissions are deduplicated by message id so the
wrapper total is never added to the per-request usage.

## Unknown is not zero

A call whose provider reports no count is still counted as a call, and it is
logged. Its tokens are not added. The bucket therefore keeps:

- `calls`: every recorded call
- `unknown`: calls with no reported count
- `tokens`: the sum of reported counts only

`tokens: 0` with `unknown > 0` means "no count reported", not "no tokens".

## Store

- File: `TOKEN_USAGE_PATH` (default `/var/lib/ai-coding-agent/token-usage.json`),
  read once at import like the other settings. Mode 0600, in the unit's
  `StateDirectory`.
- Schema:

  ```json
  {
    "format": 1,
    "days": {
      "2026-10-21": {
        "planner": {"codex": {"calls": 3, "unknown": 1, "tokens": 150}},
        "implementer": {"claude": {"calls": 2, "unknown": 2, "tokens": 0}}
      }
    }
  }
  ```

- Writes are read-modify-write under an exclusive `flock` on
  `token-usage.json.lock`, and the file is replaced atomically
  (temp file + rename). A reader sees the old file or the new one.
- The store is small: at most 90 days × 2 roles × 2 tools buckets.
- A store that is corrupt or has an unexpected shape is started again, with a
  warning. Counting never raises: a failed write is logged and the plan or run
  continues.

## Retention: 90 days

- A window is 90 UTC days, counting today as the first.
- Every write prunes days outside the window.
- The snapshot also leaves out days outside the window, so a store that has
  not been pruned yet is never published past 90 days.

## Snapshot section

`ai_agent/bot/snapshot_publisher.py` adds `token_usage` to the Coding snapshot
and bumps `format` from 1 to 2. Existing keys are unchanged.

```json
"token_usage": {
  "window_days": 90,
  "days": [
    {
      "date": "2026-10-21",
      "usage": [
        {"role": "planner", "tool": "codex", "calls": 3, "unknown": 1, "tokens": 150}
      ]
    }
  ]
}
```

- Days are oldest first. Rows follow the fixed order planner, then implementer;
  within a role, codex then claude.
- Only these keys appear: `window_days`, `days`, `date`, `usage`, `role`,
  `tool`, `calls`, `unknown`, `tokens`. The builder copies whitelisted fields
  only, and the store validator rejects any extra key, so an unexpected field
  makes the section unavailable rather than leaking.
- If the store is missing or unreadable, the `token_usage` key is omitted and
  the snapshot publishes without it. The reason is logged once per distinct
  reason, not on every 3-second tick. A missing store is logged at INFO, since
  it is normal before the first call; an unreadable one at WARNING.

## Compatibility for readers

- Format 1 readers still see every key they knew.
- Readers must treat `token_usage` as optional. The key is absent when there
  is no readable store, and format 1 snapshots never have it.
- Readers must ignore roles and tools they do not recognise.

## Versioned per-call details

`ai_agent/usage_details.py` keeps a second store (`USAGE_DETAILS_PATH`, default
`/var/lib/ai-coding-agent/usage-details.json`) with one record per provider
call. The legacy format-1 aggregate store above is not changed.

Each `UsageDetail` holds:

| Field                 | Meaning                                                              |
|-----------------------|----------------------------------------------------------------------|
| `role`                | `planner` or `implementer`                                           |
| `runner`              | `codex`, `claude-api` or `claude-code`                               |
| `provider`            | `codex`, `claude`, `claude-code` or `deepseek` (the actual provider, from the reported model id) |
| `model`               | actual model id, or `None` when unknown/CLI-default                  |
| `day`                 | UTC calendar day, `YYYY-MM-DD`                                       |
| `input_tokens`        | input tokens, or `None`                                              |
| `output_tokens`       | output tokens, or `None`                                             |
| `cache_read_tokens`   | cache reads, or `None`                                               |
| `cache_creation_tokens`| cache writes, or `None`                                              |
| `reasoning_tokens`    | reasoning tokens, or `None`                                          |
| `total_tokens`        | tool-reported total; `input + output` only, never cache/reasoning    |
| `cost`                | tool-reported cost only, never a dated estimate or subscription fee  |
| `estimate_usd`        | dated list-price estimate, or `None` — always separate from `cost`   |
| `estimate_currency`   | estimate currency (`USD`), or `None` when no rate                    |
| `estimate_source`     | the dated price-list snapshot id, or `None`                          |
| `estimate_rate`       | `peak` or `off_peak`, or `None` when no rate                         |
| `estimate_missing_rate`| `true` when the model has no rate in the snapshot (never invented)   |
| `duration_seconds`    | whole seconds of the call, or `None`                                 |
| `output_chars`        | bounded output length, never the text                                |
| `attempt_id`/`call_id`| dedupe identity, stored but never published                          |

Rules enforced by the dataclass:

- Missing fields stay `None` (unknown), never zero.
- When both `input_tokens` and `output_tokens` are known, `total_tokens` must
  equal their sum; cache and reasoning fields are never added into the total.
- `cost` accepts only a non-negative number and is only ever set from a
  tool-reported cost. No price table or subscription fee is derived from it,
  and a missing total is never turned into a dollar cap.
- The Claude planner through the API pins the Anthropic usage shape
  (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
  `cache_creation_input_tokens`); cache fields are kept separate so they are
  never silently double-counted.
- The Claude Code planner and implementer parse `--output-format stream-json`
  into a `ClaudeCodeUsage` (see `ai_agent/claude_code_usage.py`): the final
  `result` usage wins when present, unique per-turn `assistant` blocks are
  summed otherwise, and `stream_event`/`reconnect` re-emissions are
  deduplicated by message id so totals are never double-counted. The wrapper
  runner (`claude-code`) is recorded separately from the model-request
  provider/model, which derive only from the reported model id — a `deepseek*`
  id records `deepseek`, a `claude*` id records `claude`, and anything else
  records the neutral `claude-code` wrapper provider.
- The measured `cost` is only ever the tool-reported `costUSD`/`total_cost_usd`;
  the `estimate_*` fields carry a dated list-price snapshot
  (`PRICE_LIST_SOURCE` in `claude_code_usage.py`) computed from input plus
  output tokens at the snapshot's peak rate. A model absent from the snapshot
  records `estimate_missing_rate: true` and never invents a dollar figure.

The store reuses `token_usage.locked`, the atomic writer and the 90-day UTC
retention discipline, made configurable via `USAGE_DETAILS_RETENTION_DAYS`
(default 90), with a hard `USAGE_DETAILS_MAX_RECORDS` cap (default 5000). A
duplicate `(attempt_id, call_id)` is not appended twice. Cancelled
implementations still leave a record behind (unknown tokens), because the
implementer writes its detail in a `finally` block that reuses the existing
`implementation_stop`/`git_recovery` cleanup.

```json
{
  "format": 2,
  "records": [
    {
      "role": "planner",
      "runner": "claude-code",
      "provider": "deepseek",
      "model": "deepseek-v4-pro",
      "day": "2026-10-21",
      "input_tokens": 1200,
      "output_tokens": 300,
      "cache_read_tokens": 400,
      "cache_creation_tokens": 50,
      "reasoning_tokens": 100,
      "total_tokens": 1500,
      "cost": 0.042,
      "estimate_usd": 0.000588,
      "estimate_currency": "USD",
      "estimate_source": "deepseek-list-price-2026-11-09",
      "estimate_rate": "peak",
      "estimate_missing_rate": false,
      "duration_seconds": 3,
      "output_chars": 12,
      "attempt_id": "abc",
      "call_id": "def"
    }
  ]
}
```

The store still reads format-1 records (written before the estimate extension)
unchanged, upgrading them in place on the next write.

## Snapshot: usage_details section

`ai_agent/bot/snapshot_publisher.py` bumps `format` from 2 to 3 and adds the
optional `usage_details` section next to `token_usage`. The section holds only
the published fields above: `attempt_id` and `call_id` are excluded, and no
prompt or output text is ever stored, so the snapshot stays secret-free.

```json
"usage_details": {
  "window_days": 90,
  "records": [
    {
      "role": "planner",
      "runner": "claude-code",
      "provider": "deepseek",
      "model": "deepseek-v4-pro",
      "day": "2026-10-21",
      "input_tokens": 1200,
      "output_tokens": 300,
      "cache_read_tokens": 400,
      "cache_creation_tokens": 50,
      "reasoning_tokens": 100,
      "total_tokens": 1500,
      "cost": 0.042,
      "estimate_usd": 0.000588,
      "estimate_currency": "USD",
      "estimate_source": "deepseek-list-price-2026-11-09",
      "estimate_rate": "peak",
      "estimate_missing_rate": false,
      "duration_seconds": 3,
      "output_chars": 12
    }
  ]
}
```

Readers must treat `usage_details` as optional, exactly like `token_usage`.

## Tests

- `tests/conftest.py` points `TOKEN_USAGE_PATH` at a temp file before any test
  imports config, so no test writes to `/var/lib`.
- `tests/test_token_usage.py` covers parsing, validation, bucket aggregation,
  unknown counts, 90-day pruning, atomic writes and recovery, missing or
  malformed usage, and the secret-free section shape.
- `tests/test_snapshot_usage.py` covers format 2, the optional section, and a
  missing store still publishing. It imports the publisher, so it needs the
  full dependencies (`requirements-dev.txt`).
- `tests/test_claude_code_usage.py` replays the sanitized stream fixtures under
  `tests/fixtures/claude_code/` (success/cancel/fail/stream/plan) and asserts
  the deduplicated totals match each fixture's independent audit block, that
  provider/model derive only from the reported model id, that estimates come
  only from the dated list price, and that no raw content or secret ever
  reaches the parsed result.
- `tests/test_usage_details.py` covers the estimate fields, the measured-charge
  vs dated-estimate separation, and reading/upgrading a legacy format-1 store.
