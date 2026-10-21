# token_usage.py

Counts provider calls per day, role and tool, so the owner can see how much
planning and implementation consume. The counts are stored locally and
published, secret-free, in the Coding snapshot for the separate
`ai-dashboard` service.

Part 1 (this change) records and publishes the data. Part 2, in `ai-dashboard`,
reads the `token_usage` section. Until then nothing displays it.

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
| planner / claude     | Anthropic response `usage.input_tokens + usage.output_tokens` | Read from the SDK response shape; cache reads and writes are reported separately and excluded |
| planner / codex      | `tokens used` line in `codex exec` output                | **Unverified** against the installed CLI (0.162.1); no fixture here |
| implementer / codex  | same `tokens used` parse, from the implementation run   | **Unverified**, as above |
| implementer / claude | none: `claude -p` prints text, which carries no count    | Always unknown |

"Unverified" means the parser is written to the format Codex is believed to
print, but no test or fixture pins that output. To check it on the server, run
a trivial `codex exec` and confirm the `tokens used` line and its number. If
the line is absent or differs, every Codex call is counted as unknown; nothing
is recorded as zero.

Claude Code's implementer runs stay unknown because adding
`--output-format json` would change how the bot reads the implementer's
output. That is a separate change.

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

## Tests

- `tests/conftest.py` points `TOKEN_USAGE_PATH` at a temp file before any test
  imports config, so no test writes to `/var/lib`.
- `tests/test_token_usage.py` covers parsing, validation, bucket aggregation,
  unknown counts, 90-day pruning, atomic writes and recovery, missing or
  malformed usage, and the secret-free section shape.
- `tests/test_snapshot_usage.py` covers format 2, the optional section, and a
  missing store still publishing. It imports the publisher, so it needs the
  full dependencies (`requirements-dev.txt`).
