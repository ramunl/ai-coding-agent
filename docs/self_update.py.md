# self_update.py

Detached-restart helper. `schedule_restart()` is used after operations that
change the running process's own on-disk config and need the service to pick
it up (e.g. `/model <tool> set <name>`).

## Why the restart is detached

A process cannot survive its own `systemctl restart`: the handler would be
killed before its reply is sent. `schedule_restart()` therefore creates a
transient systemd unit (outside this service's cgroup) that sleeps a few
seconds and restarts the service — after the bot has already replied.
Fallback: a double-forked shell when systemd-run is unavailable.

## Configuration

- `AGENT_SERVICE_NAME` (default `ai-agent`)
- `AGENT_RESTART_DELAY_SECONDS` (default `3`)

## History

This module used to also host a tests-gated self-update flow behind `/pull`
(fetch/reset/test/restart on the agent's own repo). It was removed: the repo
already auto-deploys via GitHub Actions + a server webhook (see
[deployment.md](deployment.md)), so a second Telegram-triggered deploy path
was redundant. `/pull` now runs `git pull` on the **active project** instead
— see [telegram_bot.py.md](telegram_bot.py.md).
