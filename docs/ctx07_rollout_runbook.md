# CTX07 rollout runbook: backup -> deploy idle -> rollback

CTX07 rolls out the autonomous-DeepSeek gate behind a flag that is **off by
default**. The flag (`AUTONOMOUS_DEEPSEEK_ENABLED`) is read only by the
autonomous worker's tool picker (`ai_agent/worker.py`), so manual `/plan` is
never affected. This runbook covers backing up protected state, deploying while
the agent is idle, and rolling back in a way that preserves the context archive.

## What the gate does

- `AUTONOMOUS_DEEPSEEK_ENABLED` unset or `false` (default): the worker skips
  `deepseek` and uses a wired tool, or waits when only DeepSeek is available.
- `true`: the worker may pick `deepseek`.
- The flag never changes manual planning: `planner.py` and
  `planning_provider.py` do not read the worker gate.

**CTX08 is the durable-enable gate.** CTX08 must verify a real backup and an
isolated restore of the context archive before the flag is enabled in
production. This runbook does not enable durable context early, and it does not
wire the DeepSeek provider (that is CTX03-CTX06).

## Protected state

- `/var/lib/ai-coding-agent/state.json` — restart-safe bot state (task queue,
  pending plan, provider choices).
- `/var/lib/ai-coding-agent/context/` — the durable context archive
  (`CONTEXT_ROOT`). Archive directories are mode 0700 and records 0600.

The context archive is **outside the current 20 MiB backup coverage**. This
runbook backs it up by hand for the trial and documents the gap; it does not
change backup limits or the archive size.

## 1. Back up before the trial

Use a timestamped, owner-only backup directory and copy both files preserving
permissions:

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="/var/backups/ai-coding-agent-ctx07-$STAMP"
install -d -m 0700 "$BACKUP_DIR"
cp -a /var/lib/ai-coding-agent/state.json "$BACKUP_DIR/state.json"
cp -a /var/lib/ai-coding-agent/context "$BACKUP_DIR/context"
```

Record the baseline so the rollback check has something to compare against:

```bash
du -sb /var/lib/ai-coding-agent/context > "$BACKUP_DIR/size.before"
find /var/lib/ai-coding-agent/context -type f | sort > "$BACKUP_DIR/files.before"
```

Confirm the archive copy is complete before proceeding:

```bash
diff -r /var/lib/ai-coding-agent/context "$BACKUP_DIR/context"
```

## 2. Deploy idle

A deploy restarts the agent and kills any run in progress, so deploy only when
the worker is idle and no run is active. Confirm the worker is idle first, then
deploy from the Ops bot:

```text
/ai_update coding
```

(Equivalent: run the "Deploy AI Agent" workflow on `main` in GitHub Actions.)

Check the service came back healthy:

```bash
systemctl status ai-coding-agent.service --no-pager
tail -50 /var/log/ai-coding-agent/update.log
```

## 3. Enable the flag only after CTX08 passes

In `/etc/ai-coding-agent/ai-coding-agent.env`, set:

```text
AUTONOMOUS_DEEPSEEK_ENABLED=true
```

then deploy again via `/ai_update coding` so the service reads the new value.
Leave the line absent or `false` and the gate stays closed.

## 4. Verify rollback preserves the archive

To roll back, set the flag off again:

```text
AUTONOMOUS_DEEPSEEK_ENABLED=false
```

then `/ai_update coding`. After the restart, compare the archive against the
pre-trial baseline:

```bash
du -sb /var/lib/ai-coding-agent/context > "$BACKUP_DIR/size.after"
find /var/lib/ai-coding-agent/context -type f | sort > "$BACKUP_DIR/files.after"
diff "$BACKUP_DIR/files.before" "$BACKUP_DIR/files.after"
diff "$BACKUP_DIR/size.before" "$BACKUP_DIR/size.after"
diff -r "$BACKUP_DIR/context" /var/lib/ai-coding-agent/context \
  && echo "archive unchanged"
```

If any diff shows drift, stop and inspect before touching live data. Restoring
from the backup must not happen over live state while the bot is running: stop
the service, restore `state.json` and `context/` from `$BACKUP_DIR`, then start
it again and re-check the service.
