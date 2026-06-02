# ai_agent

Telegram coding agent for the Channel Cast Android repository.

## Required environment

Set these values in the systemd `EnvironmentFile`:

- `TELEGRAM_BOT_TOKEN`
- `YOUR_CHAT_ID`
- `ANTHROPIC_API_KEY`
- `REPO_PATH`
- `GITHUB_TOKEN`

Optional values:

- `ANTHROPIC_MODEL`, defaults to `claude-sonnet-4-20250514`
- `GITHUB_REPOSITORY`, defaults to `ramunl/com.randrgames.channelcast`
- `GITHUB_BASE_BRANCH`, defaults to `main`
- `COMMAND_TIMEOUT_SECONDS`, defaults to `120`
- `CODEX_TIMEOUT_SECONDS`, defaults to `1800`
- `CI_POLL_INTERVAL_SECONDS`, defaults to `30`
- `CI_TIMEOUT_SECONDS`, defaults to `1800`
- `CI_FIX_ATTEMPTS`, defaults to `1`
- `LINK_ALLOWED_DOMAINS`, comma-separated generic web domains to fetch, defaults to
  `developer.android.com,docs.github.com,kotlinlang.org,stackoverflow.com`

`/limits` uses Anthropic response headers, so it shows Claude API rate-limit budgets for the configured
`ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. It consumes one tiny Claude API request each time it runs.
`/codex` reports local Codex CLI/login status only; Codex ChatGPT plan limits remaining are not exposed
by the Codex CLI or a public API.
`/plan`, `/implement`, and `/bugfix` can include GitHub issue, pull request, file, or commit links. The
agent also fetches generic web links from `LINK_ALLOWED_DOMAINS`; `/plan` and `/implement` pass that
context through Claude planning, while `/bugfix` first asks clarification questions when needed and then
sends the bug-fix prompt directly to Codex without a planning step.

The default feature workflow is plan-first:

```text
/plan <feature>
/discuss <feedback>
/approve
/confirm
```

`/plan` creates an editable pending plan. `/discuss` revises it and increments the revision. `/approve`
marks the current revision as ready but does not start implementation. `/confirm` runs Codex, pushes the
branch, opens a PR, and polls CI. If CI fails, the agent reads the build failure context, runs Codex on
the same pushed branch to repair the errors, pushes the fix, and polls CI again. `/implement` keeps the
old shortcut behavior by creating an approved plan and waiting for `/confirm`.

Implementation output is quiet by default. The bot sends concise status and completion messages, then
keeps diffs and logs available through `/diff`, `/show`, `/logs`, and `/pr`.

Use `/fixpr <pr-number>` when an existing open PR in the configured repository is already failing CI.
The bot checks the PR head commit, repairs failures on that PR branch, pushes a fix commit, and polls
the new build status. PRs from forks are rejected because the bot can only push to branches on `origin`.

`GITHUB_TOKEN` needs access to create pull requests and read GitHub Actions:

- Contents: read/write
- Pull requests: read/write
- Actions: read

## Commands

Planning and implementation:

- `/plan <feature>` - create an editable implementation plan.
- `/discuss <feedback>` - revise the current pending plan.
- `/approve` - approve the current plan without implementing it.
- `/showplan` - show the current pending plan.
- `/history` - show previous plan revisions.
- `/implement <feature>` - plan, approve, and wait for `/confirm`.
- `/bugfix <bug>` - ask clarification questions if needed, then wait for `/confirm` on a `bugfix/` branch.
- `/answer <details>` - answer pending `/bugfix` clarification questions.
- `/confirm` - run approved work quietly, commit/push branch, open PR, poll GitHub Actions, and auto-repair failed CI up to `CI_FIX_ATTEMPTS`.
- `/fixpr <pr-number>` - repair failed CI on an existing same-repository PR branch.
- `/cancel` - discard the pending implementation or plan.

Output detail and inspection:

- `/verbosity concise|normal|debug` - control output detail, defaults to `concise`.
- `/diff` - show the changed-file list and line counts from the last implementation.
- `/show <file-number>` - show one file diff from the last implementation.
- `/pr` - show the last PR URL.
- `/logs [lines]` - show last implementation logs in debug mode, or service logs when no run exists.

Utilities:

- `/ci <pr-number>` - show current GitHub Actions result for a PR.
- `/limits` - show remaining Claude API rate limits.
- `/codex` - show Codex CLI/login status.
- `/test` - run agent unit tests.
- `/branches` - list repository branches.
- `/status` - show the active implementation branch/phase, or git status when idle.

## Verbosity

- `concise` - completion summary only. Hides raw code, diffs, logs, and Codex output.
- `normal` - includes changed file names in completion messages. Still hides raw code and logs.
- `debug` - includes Codex output, logs, and full diff details for troubleshooting.

## Examples

Plan, revise, approve, and run:

```text
/plan Add Chromecast queue support
/discuss Use Google Cast MediaQueue instead of a custom queue manager
/showplan
/approve
/confirm
```

Inspect the last run only when needed:

```text
/diff
/show 1
/verbosity debug
/logs
/pr
```

GitHub links can be included directly:

```text
/plan fix https://github.com/ramunl/com.randrgames.channelcast/issues/12
/implement https://github.com/ramunl/com.randrgames.channelcast/pull/34
/bugfix crash when opening https://github.com/ramunl/com.randrgames.channelcast/issues/12
/answer happens after rotating the screen; expected playback to continue
/plan update API usage from https://developer.android.com/guide
/plan inspect https://github.com/ramunl/com.randrgames.channelcast/blob/main/app/build.gradle.kts
```

## Tests

Run the unit tests with dummy environment values:

```bash
TELEGRAM_BOT_TOKEN=t YOUR_CHAT_ID=1 ANTHROPIC_API_KEY=k python -m unittest discover -v
```

## Deployment

This repo auto-deploys from `main` through GitHub Actions and the server webhook.

Flow:

```text
git push origin main
-> GitHub Actions deploy workflow
-> http://161.35.17.201:9000/hooks/ai-agent-update?secret=...
-> /usr/local/sbin/update-ai-agent
-> git pull, install requirements, restart ai-agent.service
```

The server uses the distro `webhook.service` with `/etc/webhook.conf`. It listens on `*:9000`, and UFW must allow `9000/tcp` for GitHub-hosted runners to reach it.

Details are in [docs/deployment.md](docs/deployment.md).

## Maintenance Rules

- When adding or changing a feature, update the relevant markdown docs in the same change.
- Keep `README.md` current for user-facing behavior, commands, configuration, and deployment notes.
- Keep `docs/*.md` current for module-level behavior and operational details.
