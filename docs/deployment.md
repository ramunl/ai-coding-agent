# Deployment

## Overview

`ai-coding-agent` deploys automatically when changes are pushed to `main`.

The server directory is `/opt/ai-coding-agent`, and the systemd service is `ai-coding-agent.service`
(renamed 2026-09-18 from `ai-agent.service` to match the repo name; the env file, webhook hook id,
update script, and log path were renamed to match at the same time).

```text
git push origin main
-> GitHub Actions
-> deploy webhook
-> update script
-> ai-coding-agent.service restart
```

## GitHub Actions

Workflow file:

```text
.github/workflows/deploy.yml
```

The workflow runs on pushes to `main` and can be dispatched manually. It validates
HTTPS and sends the token in an Authorization header. HTTP errors and redirects
fail the job.

Required GitHub Actions secrets:

```text
DEPLOY_WEBHOOK_URL    https://<deploy-host>/hooks/ai-coding-agent-update
DEPLOY_WEBHOOK_TOKEN  <matching webhook token>
```

Do not put credentials or query parameters in the URL. The server hook must
validate `Authorization: Bearer <matching webhook token>`.

## Server Webhook

The server uses the distro `webhook.service`, not a separate `ai-coding-agent-webhook.service`.

Service:

```text
webhook.service
```

Config:

```text
/etc/webhook.conf
```

Hook id:

```text
ai-coding-agent-update
```

Command executed:

```text
/usr/local/sbin/update-ai-coding-agent
```

Both server files are versioned in `deploy/`; install them after changing either:

```bash
install -m 755 deploy/update-ai-coding-agent /usr/local/sbin/update-ai-coding-agent
# Merge deploy/webhook.conf.example into /etc/webhook.conf, replacing
# PASTE_WEBHOOK_SECRET with the real secret, then:
systemctl restart webhook.service
```

The update script logs to `/var/log/ai-coding-agent/update.log`.

Expected listener:

```text
127.0.0.1:9000 users:(("webhook",...))
```

Check with:

```bash
ss -tlnp | grep 9000
```

## Firewall

GitHub-hosted runners connect to the HTTPS reverse proxy on TCP port 443.
After all deployment hooks have migrated and been verified, bind the webhook to
loopback and close public access to port 9000. Keep SSH allowed when changing
firewall rules.

## State Directory

Both runtime files live in `/var/lib/ai-coding-agent`:

```text
/var/lib/ai-coding-agent/state.json     restart-safe bot state (task queue, pending plan)
/var/lib/ai-coding-agent/snapshot.json  read model for the ai-dashboard service
```

`StateDirectory=ai-coding-agent` in the unit file makes systemd create the
directory before the bot starts. Override either path with `AGENT_STATE_FILE`
or `AGENT_SNAPSHOT_FILE` in `/etc/ai-coding-agent/ai-coding-agent.env`.

`state.json` holds prompts and diffs, so it stays private to the bot.
`snapshot.json` is secret-free and written mode 0600; the dashboard service
reads it and treats it as stale after 90 s. See `docs/snapshot_publisher.py.md`.

Check both after a deploy:

```bash
ls -l /var/lib/ai-coding-agent
```

## Verification

Push an empty commit to `main`:

```bash
git commit --allow-empty -m "verify deploy webhook"
git push origin main
```

Then check the update log:

```bash
tail -100 /var/log/ai-coding-agent/update.log
```

Successful deploy log shape:

```text
[YYYY-MM-DDTHH:MM:SS+00:00] update started
...
[YYYY-MM-DDTHH:MM:SS+00:00] update finished
```

Check the service:

```bash
systemctl status ai-coding-agent.service --no-pager
```

## Historical Deployment State (before HTTPS migration)

Verified on 2026-09-18:

- Webhook hook id renamed `ai-agent-update` -> `ai-coding-agent-update`; old hook id now 404s.
- `DEPLOY_WEBHOOK_URL` GitHub secret updated to `.../hooks/ai-coding-agent-update?secret=...`.
- New service `ai-coding-agent.service` active; old `ai-agent.service` unit removed.
- Manual webhook POST to the new hook id triggered `update-ai-coding-agent` and logged to `/var/log/ai-coding-agent/update.log` successfully.

## Troubleshooting

If GitHub Actions fails with `curl: (3) URL rejected` or a malformed URL error, reset `DEPLOY_WEBHOOK_URL`.

If validation fails, the secret value does not match the expected URL components.

If `curl` fails with `Could not resolve host`, the secret host is not the expected address.

If HTTPS requests time out, check DNS, the reverse proxy, its certificate, and
public TCP port 443. Check that `webhook.service` is running and reachable by the
proxy on loopback port 9000. A 401 means the token does not match the hook.

### Deploy the dashboard from Telegram

Select `/repo_use ai-dashboard`, then run `/deploy main`. The dashboard uses
`/usr/local/sbin/update-ai-dashboard` and logs to
`/var/log/ai-dashboard/update.log`. Install that script from the dashboard
repository on the server before the first deployment. The dashboard has no
shared-core submodule, so it is excluded from `/core update`.
