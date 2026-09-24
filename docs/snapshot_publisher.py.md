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

Carries `"format": 1`. Changing the shape incompatibly means bumping it and
updating the dashboard first, which reports unknown formats instead of
misreading them. Full field list: the ai-dashboard README.
