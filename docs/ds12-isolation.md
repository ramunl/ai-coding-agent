# DS12 isolation: non-root worker, disposable sandboxes, scoped credentials

When enabled after migration, DS12 makes the implementer provider CLI run as a dedicated non-root worker in a
disposable `git worktree`, with the service's secrets stripped from its
environment. The production checkout is never handed to the provider; the
sandbox diff is re-applied only after the provider process group is verified
dead. This closes the biggest escalation path: a provider that runs
`--dangerously-skip-permissions` (or that is prompted into `sudo`) can no longer
write the production checkout, read the bot's secrets, or use the owner's
GitHub token.

## Staged activation (deployment compatibility)

Non-root provider execution is **opt-in** with
`WORKER_ISOLATION_ENABLED=true`. The default systemd unit retains the existing
controller identity and HOME, so ordinary code upgrades do not migrate service
accounts, credentials or filesystem ownership. Disposable worktrees and process
supervision remain active with the existing service identity while the flag is
off; this compatibility mode does **not** claim non-root isolation.

Provision and verify the dedicated worker's identity, readable CLI executables,
provider credentials/configuration, worktree Git access and owned directories
through Ops before enabling the flag. Root-only CLI installations and credential
paths must be migrated deliberately. Enabling the flag without an account/group
fails configuration validation; it never silently falls back to root.

Moving the controller itself to a new service account is not part of this flag.
That would also require migrating controller Git/SSH, projects, rules,
configuration and state. Do not chown the controller state or production
repositories to the provider worker as a shortcut.

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
- `deploy/ai-coding-agent.service` — existing controller identity and HOME; no
  implicit account migration during application deployment.
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

## Ops-owned rollout prerequisites

Before enabling `WORKER_ISOLATION_ENABLED=true`:

1. Back up the state/configuration and verify a known-good rollback revision.
   Wait until no planning or implementation work is active.
2. Provision the dedicated non-root user/group. Give it only its worker HOME,
   disposable worktree and required per-worktree Git metadata. Keep controller
   state, credentials and production refs private to the controller.
3. Make approved provider CLI executables readable/executable outside `/root`.
   Provision only the required provider credentials in the worker's own config.
   Verify the DeepSeek compatibility endpoint and Codex fallback independently;
   changing HOME alone does not migrate authentication or `CLAUDE_CONFIG_DIR`.
4. Verify dependency tools, subprocess UID/GID, repository reads, permitted
   edits, denied secret/outside reads/writes, images, submodules, cancellation,
   timeout and recovery in a disposable checkout.
5. Set the activation flag through the Ops-managed service environment, deploy
   idle and verify a small approved run. Missing accounts fail configuration
   validation and must never trigger an automatic root fallback.

The flag does not automatically provision accounts, copy credentials, change
ownership or install systemd hardening. The existing service remains in
compatibility mode until the rollout checks above pass. Run root-gated escape
tests explicitly on a provisioned test host; skipped tests are not evidence
that isolation works in production.

## Rollback

Use the deployment manager's verified previous release and environment backup.
Restore the previous flag value only as an explicit Ops rollback decision; a
failed isolated run must not disable isolation automatically. Preserve recovery
refs and wait for provider process shutdown before touching a checkout. Do not
chown state or production repositories to the worker or discard pending edits.
