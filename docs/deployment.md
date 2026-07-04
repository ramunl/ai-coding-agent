# Deployment

## Overview

`ai-coding-agent` deploys automatically when changes are pushed to `main`.

The server directory remains `/opt/ai-coding-agent`, and the systemd service remains `ai-agent.service`.
Those operational names do not need to change when the GitHub repository is renamed.

```text
git push origin main
-> GitHub Actions
-> deploy webhook
-> update script
-> ai-agent.service restart
```

## GitHub Actions

Workflow file:

```text
.github/workflows/deploy.yml
```

The workflow runs on pushes to `main`. It validates `DEPLOY_WEBHOOK_URL` without printing the secret, then calls the webhook:

```bash
curl --fail --show-error --silent -X POST "$DEPLOY_WEBHOOK_URL"
```

Required GitHub Actions secret:

```text
DEPLOY_WEBHOOK_URL
```

Expected value format:

```text
http://<deploy-host>:9000/hooks/ai-agent-update?secret=<webhook-secret>
```

The webhook secret is stored on the server in `/etc/webhook.conf`.

Optional stricter validation: set the repository variables `DEPLOY_WEBHOOK_HOST`
and `DEPLOY_WEBHOOK_PORT` to have the workflow assert the webhook URL points at the
expected host and port. When unset, only the scheme, path, and secret are validated.

## Server Webhook

The server uses the distro `webhook.service`, not a separate `ai-agent-webhook.service`.

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
ai-agent-update
```

Command executed:

```text
/usr/local/sbin/update-ai-agent
```

Expected listener:

```text
*:9000 users:(("webhook",...))
```

Check with:

```bash
ss -tlnp | grep 9000
```

## Firewall

GitHub-hosted runners must be able to connect to TCP port `9000` on the server.

UFW must include:

```text
9000/tcp ALLOW Anywhere
9000/tcp (v6) ALLOW Anywhere (v6)
```

Check with:

```bash
ufw status
```

Open the port if needed:

```bash
ufw allow 9000/tcp
```

## Verification

Push an empty commit to `main`:

```bash
git commit --allow-empty -m "verify deploy webhook"
git push origin main
```

Then check the update log:

```bash
tail -100 /var/log/ai-agent/update.log
```

Successful deploy log shape:

```text
[YYYY-MM-DDTHH:MM:SS+00:00] update started
...
[YYYY-MM-DDTHH:MM:SS+00:00] update finished
```

Check the service:

```bash
systemctl status ai-agent.service --no-pager
```

## Current Known-Good State

Verified on 2026-05-28:

- `DEPLOY_WEBHOOK_URL` format validation passed in GitHub Actions.
- `webhook.service` listened on `*:9000`.
- UFW allowed `9000/tcp`.
- GitHub Actions reached the webhook after the firewall rule was added.
- `/var/log/ai-agent/update.log` showed a successful update at `2026-05-28T13:50:18+00:00`.
- `ai-agent.service` restarted successfully at `2026-05-28T13:50:23+00:00`.

## Troubleshooting

If GitHub Actions fails with `curl: (3) URL rejected` or a malformed URL error, reset `DEPLOY_WEBHOOK_URL`.

If validation fails, the secret value does not match the expected URL components.

If `curl` fails with `Could not resolve host`, the secret host is not the expected address.

If `curl` times out connecting to the webhook host on port `9000`, check:

- `webhook.service` is running.
- `ss -tlnp` shows the webhook listening on `*:9000`.
- UFW allows `9000/tcp`.
- Any provider-level cloud firewall also allows inbound TCP `9000`.
