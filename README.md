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

`/limits` uses Anthropic response headers, so it shows Claude API rate-limit budgets for the configured
`ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. It consumes one tiny Claude API request each time it runs.
`/codex` reports local Codex CLI/login status only; Codex ChatGPT plan limits remaining are not exposed
by the Codex CLI or a public API.
`/plan` and `/implement` can include GitHub issue or pull request links. The agent fetches the linked
title, body, and recent comments before asking Claude to plan the work.

`GITHUB_TOKEN` needs access to create pull requests and read GitHub Actions:

- Contents: read/write
- Pull requests: read/write
- Actions: read

## Commands

- `/plan <feature>` - plan only.
- `/implement <feature>` - plan and wait for `/confirm`.
- `/confirm` - run Codex, commit/push branch, open PR, and poll GitHub Actions.
- `/ci <pr-number>` - show current GitHub Actions result for a PR.
- `/limits` - show remaining Claude API rate limits.
- `/codex` - show Codex CLI/login status.
- `/test` - run agent unit tests.
- `/cancel` - discard the pending implementation.
- `/branches` - list repository branches.
- `/status` - show git status.
- `/logs [lines]` - show recent service logs.

GitHub links can be included directly:

```text
/plan fix https://github.com/ramunl/com.randrgames.channelcast/issues/12
/implement https://github.com/ramunl/com.randrgames.channelcast/pull/34
```

## Tests

Run the unit tests with dummy environment values:

```bash
TELEGRAM_BOT_TOKEN=t YOUR_CHAT_ID=1 ANTHROPIC_API_KEY=k python -m unittest discover -v
```
