# DS12 isolation: non-root worker, disposable sandboxes, scoped credentials

DS12 makes the implementer provider CLI run as a dedicated non-root worker in a
disposable `git worktree`, with the service's secrets stripped from its
environment. The production checkout is never handed to the provider; the
sandbox diff is re-applied only after the provider process group is verified
dead. This closes the biggest escalation path: a provider that runs
`--dangerously-skip-permissions` (or that is prompted into `sudo`) can no longer
write the production checkout, read the bot's secrets, or use the owner's
GitHub token.

## What changed

- `ai_agent/config.py` — `WORKER_USER`, `WORKER_GROUP`, `WORK_ROOT`,
  `WORKER_HOME` with `validate_work_config()`. Defaults to
  `ai-coding-runner` and `/var/lib/ai-coding-agent/{work,home}`.
- `ai_agent/isolation.py` (new) — creates a detached worker-owned worktree per
  task under `WORK_ROOT`, seeds a resume checkpoint as a baseline commit, and
  produces/applies the incremental sandbox diff back to production.
- `ai_agent/provider_process.py` — the isolated provider child drops to
  `WORKER_USER`/`WORKER_GROUP` via `subprocess` `user=`/`group=` and inherits
  only the memfd image descriptors it references (`pass_fds`).
- `ai_agent/claude_env.py` — every provider CLI (`codex`, `claude`) gets the
  secret-stripped environment; the worker run also points `HOME` at
  `WORKER_HOME`. `CLAUDE_CODE_AUTH` is preserved so the claude.ai subscription
  login still works.
- `ai_agent/image_delivery.py` — image descriptors are opened and handed to the
  child through `/proc/self/fd/` so they stay readable after the uid drop.
- `ai_agent/workflow.py` — implementation runs are wrapped in
  `isolation.sandbox_for(...)`; the provider cwd is the sandbox, and the
  sandbox diff is re-applied to production on success and on failure (so
  partial edits survive recovery).
- `deploy/ai-coding-agent.service` — hardened unit (see below).
- `tests/test_isolation.py` (new) — sandbox round-trips (rename, binary,
  submodule gitlink) plus root-gated escape checks.

## How the sandbox round-trip works

1. `create_sandbox` adds `git worktree add --detach <WORK_ROOT>/ai-run-<hash>`
   at the production `HEAD`.
2. For a resume, `seed_sandbox` captures the production working tree (including
   untracked files via `git add -N .`) into the sandbox and commits it as a
   *baseline*. The post-run diff is then only the provider's new edits, which
   re-apply cleanly on top of the restored checkpoint.
3. The provider runs with cwd = sandbox. On success (or on failure, before
   `git_recovery` preserves partial edits), `apply_sandbox` re-applies
   `git diff --binary --cached <baseline>` to the production checkout.
4. `remove_sandbox` unregisters and deletes the worktree.

## Live-server change (Ops-owned — not executed by code)

1. Create the worker:
   ```bash
   useradd --system --home /var/lib/ai-coding-agent/home \
     --create-home --shell /usr/sbin/nologin ai-coding-runner
   ```
2. Give the worker its state/work directories and make the production checkout
   writable by it (the bot now runs as this user):
   ```bash
   install -d -m 0700 -o ai-coding-runner -g ai-coding-runner \
     /var/lib/ai-coding-agent/work /var/lib/ai-coding-agent/home
   chown -R ai-coding-runner:ai-coding-runner /var/lib/ai-coding-agent
   chown -R ai-coding-runner:ai-coding-runner /opt/ai-coding-agent
   ```
3. Install the hardened unit and reload:
   ```bash
   install -m 0644 deploy/ai-coding-agent.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl restart ai-coding-agent.service
   ```

The unit change is `User=ai-coding-runner` / `Group=ai-coding-runner`,
`HOME=/var/lib/ai-coding-agent/home`, `StateDirectory=` for
`ai-coding-agent`, `ai-coding-agent/work`, `ai-coding-agent/home`, and hardening
`NoNewPrivileges=yes`, `ProtectSystem=full`, `ProtectHome=yes`,
`PrivateTmp=yes`.

## Deploy idle, then verify

Deploy only when the worker is idle and no run is active (a restart kills a
run). See `docs/ctx07_rollout_runbook.md` for the backup-first pattern; for DS12
the relevant state is the same `/var/lib/ai-coding-agent` tree plus the
production checkout.

After the restart:

```bash
systemctl status ai-coding-agent.service --no-pager
sudo -u ai-coding-runner bash -c 'echo ok > /var/lib/ai-coding-agent/work/write-test && rm /var/lib/ai-coding-agent/work/write-test'
```

Run the root-gated escape checks on the host (they skip on the non-root CI
runner):

```bash
sudo python3 -m pytest -q tests/test_isolation.py
```

Confirm the Claude subscription login still works for the worker — a `/plan`
or `/implement` that reaches the Claude Code CLI must not fail with a login
prompt. The login lives in the worker HOME, not `/root`, so if the worker was
freshly created, `ai-coding-runner` must complete the one-time claude.ai login
(or Ops must copy the credential into the worker HOME) before the first Claude
run.

## Rollback

To roll back to the root service:

1. Reinstall the previous unit (or set `User=root`/remove the `User=` and
   `Group=` lines, restore `Environment=HOME=/root`, drop the hardening lines),
   then:
   ```bash
   systemctl daemon-reload
   systemctl restart ai-coding-agent.service
   ```
2. Revert ownership so the root service can write again:
   ```bash
   chown -R root:root /var/lib/ai-coding-agent /opt/ai-coding-agent
   ```
3. Leave `ai-coding-runner` in place (harmless) or remove it once nothing is
   running as it:
   ```bash
   userdel ai-coding-runner
   ```
