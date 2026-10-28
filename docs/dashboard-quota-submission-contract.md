# Quota and Bugfix contracts for dashboard part 2

`limits_snapshot()` keeps the `codex`, `claude` (API rate limits), and
`claude_code` (subscription quota) entries separate. Existing `status`, `message`,
`windows`, and successful-reading `checked_at` fields remain available.

Claude Code adds:

- `attempted_at`: Unix timestamp of the latest collection attempt, or null.
- `attempt_source`: `probe` or `status_line`, or null for legacy data.
- `source`: source of the last successful reading; null for legacy data.
- `collection_error`: null on successful collection; otherwise a sanitized code:
  `no_valid_windows`, `cli_failed`, `cli_unavailable`, `timeout`,
  `authentication_failed`, `authentication_conflict`, or `api_spending_limit`.
- `auth_conflict`: whether the latest probe reported conflicting authentication.
  It can be true alongside `api_spending_limit`.
- `freshness`: `unknown` without a successful reading, `fresh` for an unexpired
  window read within the past hour, otherwise `stale`. This describes reading
  age independently of collection failure.

A failed attempt preserves successful-reading time and quota windows on disk.
Expired windows are removed from snapshots, never reset to 100% remaining.
`status` is `unavailable` for failed collection or stale readings, even when
last-known unexpired windows remain. Display collection errors separately from
reading age; do not label a failed collection as merely an expired reading.
Status-line payloads lacking `rate_limits` are ignored; present but empty or
malformed quota data records a failed attempt. A subsequent valid reading clears
the collection error. Legacy cache files remain readable.

API rate-limit headers do not measure subscription quota or spending capacity.
New API records carry `kind: api_rate_limits` and `spending_status: unknown`.
HTTP failures without headers mark API status unavailable and preserve the old
header reading/time, with `attempted_at` and `collection_error: http_<status>`.
No spending reset or restored capacity is inferred from headers or elapsed time.
Probes consume usage; `CLAUDE_LIMITS_PROBE_MINUTES=0` disables them.

## Image submissions

Publish `uploads/<request-id>/<name>` before atomically publishing
`<request-id>.json`. A Bugfix request uses:

```json
{
  "id": "a1b2c3d4",
  "action": "start_work",
  "requested_at": 1793213697,
  "args": {
    "kind": "bugfix",
    "text": "Describe the failure and expected behavior",
    "images": [{"name": "screen.png"}]
  }
}
```

Use a new request ID for each submission. Omit `images` for text-only work;
an empty array is invalid. Images on other `start_work` kinds are rejected.
Text must be one visible line, at most 4000 characters. Existing image count,
size, format, safe-name, and 120-second request/upload limits still apply.
The filename must match the image request ID. Mismatch cleanup targets only
the filename's staging directory, never the directory named by another ID.

The inbox claims uploads once and passes durable `{id: ...}` references to the
handler. The handler retains them through clarification, persisted restart,
and queued implementation. `answer_bugfix` sends text and reuses those references.
Immediate raised dispatch errors produce a `failed` action result with sanitized
error text. Cleanup removes only claimed images not already referenced by work.
A failed assessment can therefore remain retryable via `answer_bugfix` or be
cancelled normally.

A `done` submission result acknowledges dispatch, not completed implementation.
After the first reply or the 15-second dispatch wait, accepted work may continue
in the background; later failures go to the bot chat and `last_work_error`.
The dashboard should use pending-work/task state to show subsequent progress.

## Ops follow-up (owner confirmation required)

The supplied cached October 28 journal reports both an API usage spending block
and an authentication source taking precedence over the claude.ai login. It does
not establish current server authentication or current subscription quota.
No server configuration was changed in this coding run.

If subscription authentication is intended, ask Ops to verify the current CLI
auth source and, with owner confirmation, remove the CLI's `ANTHROPIC_API_KEY`
override from the Claude subprocess environment (and any other override actually
identified by that check). Preserve the API planner's credentials in its own
environment. Do not globally unset the bot's planner API key. Any service restart
must wait until active runs finish; a verification probe consumes usage.
