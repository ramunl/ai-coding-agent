# self_update.py

Tests-gated self-update behind the /pull command. Lets you deploy small fixes
from Telegram without SSH, while making it hard to brick the bot with a bad
push.

## Flow

```
/pull
  git fetch origin main
  nothing new?            -> "Already up to date", stop
  local changes on disk?  -> refuse (never clobber server-side edits)
  git reset --hard origin/main        (disk now has NEW code)
  run unittest suite in a SUBPROCESS  (new code; the running bot still has
                                       old modules in memory)
  tests fail -> git reset --hard back to previous commit, report, NO restart
  tests pass -> reply first, then detached restart via systemd-run
```

## Why the restart is detached

A process cannot survive its own `systemctl restart`: the handler would be
killed before its reply is sent. `schedule_restart()` therefore creates a
transient systemd unit (outside this service's cgroup) that sleeps a few
seconds and restarts the service — after the bot has already replied.
Fallback: a double-forked shell when systemd-run is unavailable.

## What this does and does not protect against

Protected: syntax errors, broken imports, any regression the test suite
catches. The bad code never runs; the bot stays up on the old version.

Not protected: changes that pass tests but fail on real startup (bad env
reference, server-only conditions). Recovery for that case:
ops bot /logs ai-agent for the traceback, then on the server
`git reset --hard HEAD~1 && systemctl restart ai-agent`.
A watcher unit with verify-then-rollback can close this gap later if /pull
becomes the main deploy path.

## Operational notes

- A permanently red test suite makes /pull reject everything — keep main
  green. (A stale test asserting on the removed agent.HELP_TEXT was deleted
  for exactly this reason.)
- `AGENT_SERVICE_NAME` (default ai-agent) and `AGENT_RESTART_DELAY_SECONDS`
  (default 3) are configurable via env.
- /pull updates the AGENT's own repo (AGENT_DIR), not the active project.
  It is unrelated to /repo_use.
