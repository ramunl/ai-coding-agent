# snapshot_publisher.py

Publishes the coding agent's read model to
`/var/lib/ai-coding-agent/snapshot.json` (override: `AGENT_SNAPSHOT_FILE`) for
the separate **ai-dashboard** service, which shows it as the Coding window.

## Why a file

The dashboard runs as its own service so it keeps working when this bot
crashes or restarts. It never talks to this process; the snapshot file is the
whole contract between them.

Reading `state.json` instead would not work: it holds prompts and diffs the
dashboard should not see, and it deliberately never stores the running task
(`active_execution` is transient), so "Now" would always look idle.

## Behaviour

- Content is `snapshot()` (secret-free) plus the active project and versions.
- Written when the content changes, and at least every 30 s as a heartbeat,
  so the dashboard can tell "idle" from "stopped publishing" (stale after 90 s).
- Atomic write (temp file + rename), mode 0600.
- Versions are read once at startup; only a deploy changes them, and a deploy
  restarts the bot. The active project is re-read every tick (`/repo_use`).
- A failing tick is logged and retried; publishing never blocks or crashes the bot.

## Format

Carries `"format": 2`. Changing the shape incompatibly means bumping it and
updating the dashboard first, which reports unknown formats instead of
misreading them. Full field list: the ai-dashboard README.

Format 2 added the optional `token_usage` section (daily call and token counts
by role and tool, 90 days). It is omitted when the usage store is missing or
unreadable, and the rest of the snapshot still publishes. See
[token usage](token_usage.md).

Image usage is published as `image_storage: {used_bytes, cap_bytes, count}`.
Task views include `image_count`; references, bytes and local paths are excluded.
See [task image contract](task_images.md).

## Authentication contract

Additive format-2 field: `setup.models[].auth`. Existing tool IDs (`codex`,
`claude-code`, `claude`) and model fields are unchanged. Dashboard rendering is
outside this repository. Example:

```json
{"status":"authenticated","method":"oauth","checked_at":1793132265.0,
 "stale":false,"relogin_at":null,"days_remaining":null,
 "countdown_support":"no_authoritative_deadline","warnings":[]}
```

- `status`: `authenticated` (CLI reports OAuth login), `logged_out` (explicit
  absence), `configured_unverified` (configuration without validation),
  `unknown` (unrecognized/malformed evidence), or `unavailable` (check cannot
  run). Authentication does not imply quota or successful inference.
- `method`: `oauth`, `api_key`, `api_key_helper`, `third_party`, `none`, or
  `unknown`. No provider-supplied free text is copied.
- `checked_at`: Unix seconds of the observation, or null before any check.
  Failed retries retain the prior observation and its timestamp.
- `stale`: true before the first check, immediately on failure, or at age
  >= 900 seconds. Polling runs every 300 seconds after completion in a separate
  task. Each CLI invocation has a five-second timeout and a combined stdout /
  stderr limit of 16 KiB. Timeout, output overflow and cancellation kill the
  process group and reap the child. Publisher shutdown cancels the poller.
- `relogin_at`: authoritative mandatory relogin time in Unix seconds, else
  null. `days_remaining`: ceiling of remaining seconds / 86400, clamped to
  zero, else null. A refreshable token expiry is never a relogin deadline.
- `countdown_support`: `no_authoritative_deadline` explains null countdowns;
  `supported` is reserved for an adapter with a verified mandatory deadline.
  **No current adapter supplies such a deadline.** Do not invent a duration
  from login age, token expiry, or a provider's usual session length.
- `warnings`: stable codes. `login_required` means explicit logged-out status
  or a confirmed deadline reached; `relogin_soon` means a confirmed deadline
  within seven days inclusive (but still in the future); `check_unavailable`
  means unknown/unavailable evidence or a failed retry; `auth_stale` means the
  freshness rules above apply. Codes may coexist. Stale observations and their
  warnings must be displayed as historical/unverified, not current certainty.

Snapshot ticks only copy cached records and recompute freshness/countdowns.
Cache state is memory-only and starts empty after restart. Account identifiers,
credential paths, keys, key fragments, tokens, raw errors, and raw CLI output
are excluded by adapter field/value allow-lists. Check timestamps are separate
from the setup and snapshot publication timestamps.
