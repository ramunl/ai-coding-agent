# ai-coding-agent

Telegram coding agent that plans and implements features/bugfixes across registered project repositories.

## Claude Code limits

Claude Code reports its subscription limits (5-hour and weekly windows) to
the status line, which only an interactive session runs, so with the bot using
`claude -p` the reading went stale. The bot now probes every 30 minutes: one
tiny `claude -p` turn on the cheapest model with `--output-format
stream-json`, whose `rate_limit_event` carries the same windows; they are saved
where the status line writes them, for the dashboard and the worker's limit
checks. A probe that finds nothing keeps the last reading.
`CLAUDE_LIMITS_PROBE_MINUTES` (0 = off) and `CLAUDE_LIMITS_PROBE_MODEL`
(default `haiku`) change it.

## Project brief

`AGENTS.md` describes this repository for AI agents (Codex reads it, and
`CLAUDE.md` points Claude Code to it). The planner puts the active project's
`AGENTS.md` (or `CLAUDE.md`, else the start of its README) into every plan,
revision and bugfix prompt, before the coding rules; keep one in each
registered repository.

The planner also gets a map of every registered repository (the first
paragraph of each one's `AGENTS.md` or README, plus `ecosystem.md` at the root
of the ai-rules repo when present: who owns which screen, command or data).
Its plan lists the repositories the change needs, in order. If that is not
just the active project, the plan says so before you approve: approving
implements only the active project, and a plan that doesn't touch it at all
can't be approved here.

`/split` (or **Split into tasks** in the dashboard) turns a plan that needs
several repositories into one To do task per repository, in the plan's order.
Each part is planned in its own repository with the whole change as context,
and gets its own branch and pull request. A part waits until the part before
it has a pull request; the worker follows the same order. The todo the plan
came from goes with the last part, so it closes when the whole change is
deployed.

A plan for one repository that isn't the active project (the task was made in
the wrong one) is moved the same way: `/split` (**Move to <repository>** in the
dashboard) makes one To do task there, planned again in that repository.

## Required environment

Set these values in the systemd `EnvironmentFile`:

- `TELEGRAM_BOT_TOKEN`
- `YOUR_CHAT_ID`
- `REPO_PATH`
- `GITHUB_TOKEN`

Optional values:

- `PLANNING_AGENT`, `codex` or `claude`, defaults to `codex`
- `ANTHROPIC_API_KEY`, required only when Claude is selected for planning
- `CODEX_MODEL`: optional model override for all Codex planning and implementation runs.
  Set it in **Coding → Setup → AI tools**, or `/model codex set <id>`;
  `/model codex list` lists the installed CLI catalog. `default` restores CLI
  configuration. Selection is saved to `AGENT_ENV_FILE` and applies to the next
  run without a restart. Listing/validation uses `model/list`, with no inference.
- `ANTHROPIC_MODEL`, defaults to `claude-sonnet-4-6`
- `IMPLEMENTATION_AGENT`, `codex` or `claude`, defaults to `codex`
- `CLAUDE_CODE_ARGS`, extra Claude Code CLI args, defaults to `--permission-mode acceptEdits`;
- `CLAUDE_CODE_ALLOWED_TOOLS`, comma-separated commands Claude Code may run without asking (it can't ask in `claude -p`); defaults to tests (`pytest`, `npm test`, `node --test`), `ruff`, read-only `git` and read-only `gh run`/`gh pr`. Empty turns it off; an `--allowedTools` in `CLAUDE_CODE_ARGS` replaces it;
  unsafe permission-bypass flags are removed when the service runs as root
- `GITHUB_REPOSITORY`, defaults to `owner/repo`
- `GITHUB_BASE_BRANCH`, defaults to `main`
- `COMMAND_TIMEOUT_SECONDS`, defaults to `120`
- `CODEX_TIMEOUT_SECONDS`, defaults to `1800`
- `CI_POLL_INTERVAL_SECONDS`, defaults to `30`
- `CI_TIMEOUT_SECONDS`, defaults to `1800`
- `CI_FIX_ATTEMPTS`, defaults to `3`
- `LINK_ALLOWED_DOMAINS`, comma-separated generic web domains to fetch, defaults to
  `developer.android.com,docs.github.com,kotlinlang.org,stackoverflow.com`

`/limits` shows both providers by default. Claude reports rate-limit headers for the configured
`ANTHROPIC_API_KEY` and consumes one tiny API request when checked. Codex reports CLI/login readiness,
and the dashboard Limits card shows cached ChatGPT account quota from the app-server.
`/agent codex|claude` chooses whether queued implementations and CI repairs run through Codex or
Claude Code. `/planner codex|claude` independently chooses the provider used by `/plan`, `/discuss`,
`/implement`, and bug triage. Codex planning uses the local ChatGPT-authenticated CLI in read-only mode;
Claude remains available when `ANTHROPIC_API_KEY` is configured. The Telegram selection lasts until the
bot restarts; `PLANNING_AGENT` controls the startup default.
`/codex` reports local Codex CLI/login status only; Codex ChatGPT quota windows are shown in the dashboard Limits card.
`/plan`, `/implement`, and `/bugfix` can include GitHub issue, pull request, file, or commit links. The
agent also fetches generic web links from `LINK_ALLOWED_DOMAINS`; `/plan` and `/implement` pass that
context through the selected planner, while `/bugfix` first asks clarification questions when needed and then
sends the bug-fix prompt directly to Codex without a planning step.

The default feature workflow is plan-first:

```text
/plan <feature>
/discuss <feedback>
/approve
/confirm
```

`/plan` creates an editable pending plan. `/discuss` revises it and increments the revision. `/approve`
marks the current revision as ready but does not start implementation. `/confirm` adds the approved work
to a FIFO queue and starts draining queued tasks when no runner is active. Each task runs Codex, pushes
the branch, opens a PR, and polls CI. If CI fails, the agent reads the build failure context, runs Codex
on the same pushed branch to repair the errors, pushes the fix, and polls CI again. If the final repair
attempt still fails, the bot reports the exhausted repair count and marks the implementation as failed.
`/implement` keeps the old shortcut behavior by creating an approved plan and waiting for `/confirm`.

The queue is intentionally sequential because the bot uses one git working tree. `/queue` shows the
running task and pending FIFO tasks. `/cancel <task-id>` removes a queued task that has not started.

Implementation output is quiet by default. The bot sends concise status and completion messages, then
keeps diffs and logs available through `/diff`, `/show`, `/logs`, and `/pr`.

Use `/fixpr <pr-number>` when an existing open PR in the configured repository is already failing CI.
The bot checks the PR head commit, repairs failures on that PR branch, pushes a fix commit, and polls
the new build status. If the repair attempt budget is exhausted, the bot reports that CI is still
failing. PRs from forks are rejected because the bot can only push to branches on `origin`.

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
- `/confirm` - enqueue approved work, run queued tasks quietly, open PRs, poll GitHub Actions, and auto-repair failed CI up to `CI_FIX_ATTEMPTS`.
- `/queue` - show the running task and pending FIFO queue.
- `/planner [codex|claude]` - show or choose the AI used for planning and bug triage.
- `/agent [codex|claude]` - show or choose the AI used for queued implementations and CI repairs.
- `/fixpr <pr-number>` - repair failed CI on an existing same-repository PR branch.
- `/cancel [task-id]` - discard the pending implementation or plan, or remove a queued task.

Output detail and inspection:

- `/verbosity concise|normal|debug` - control output detail, defaults to `concise`.
- `/diff` - show the changed-file list and line counts from the last implementation.
- `/show <file-number>` - show one file diff from the last implementation.
- `/pr` - show the last PR URL.
- `/logs [lines]` - show last implementation logs in debug mode, or service logs when no run exists.

Utilities:

- `/ci <pr-number>` - show current GitHub Actions result for a PR.
- `/limits [all|codex|claude|planner|agent]` - show all providers or the provider selected for a role.
- `/codex` - show Codex CLI/login status.
- `/test` - run agent unit tests.
- `/branches` - list repository branches.
- `/status` - show the running task and pending queue, or git status when idle.

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

Inspect or adjust queued work:

```text
/queue
/cancel 2
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
/plan fix https://github.com/owner/repo/issues/12
/implement https://github.com/owner/repo/pull/34
/bugfix crash when opening https://github.com/owner/repo/issues/12
/answer happens after rotating the screen; expected playback to continue
/plan update API usage from https://developer.android.com/guide
/plan inspect https://github.com/owner/repo/blob/main/app/build.gradle.kts
```

## Tests

Run the unit tests with dummy environment values:

```bash
TELEGRAM_BOT_TOKEN=t YOUR_CHAT_ID=1 ANTHROPIC_API_KEY=k python -m unittest discover -v
```

## Development and architecture

Follow the [Python coding rules](https://github.com/ramunl/ai-rules/blob/main/global/python.md).
Telegram commands are grouped by responsibility under `ai_agent/bot`; see the
[module map and execution flow](docs/telegram_bot.py.md). `ai_agent/telegram_bot.py`
contains application wiring and command dispatch.

Install development dependencies and run the same checks as CI:

```bash
python -m pip install -r requirements-dev.txt
ruff check agent.py ai_agent tests
ruff format --check agent.py ai_agent tests
ruff check ai_agent/bot ai_agent/telegram_bot.py --select ANN001,ANN201,ANN202,D100,D103
python -m pytest -q
```

Before committing, apply formatting with `ruff format agent.py ai_agent tests`.
The repository uses Python 3.12 and an 88-character formatter target. The shared
core remains pinned as a submodule; initialize it with
`git submodule update --init --recursive` before running tests.

## Deployment

Merges to `main` do not deploy by themselves: a deploy restarts the agent and
kills a run in progress. Deploy when it is idle, with `/ai_update coding` in the
Ops bot or **Run workflow** on the "Deploy AI Agent" action.

Flow (Run workflow):

```text
Run workflow on main
-> GitHub Actions deploy workflow
-> HTTPS deploy webhook (Authorization: Bearer token)
-> /usr/local/sbin/update-ai-coding-agent
-> git pull, install requirements, restart ai-coding-agent.service
```

The server uses `webhook.service` with `/etc/webhook.conf`, behind the HTTPS
endpoint configured in `DEPLOY_WEBHOOK_URL`. The workflow authenticates with
`DEPLOY_WEBHOOK_TOKEN` in an Authorization header.

Details are in [docs/deployment.md](docs/deployment.md).

## Maintenance Rules

- When adding or changing a feature, update the relevant markdown docs in the same change.
- Keep `README.md` current for user-facing behavior, commands, configuration, and deployment notes.
- Keep `docs/*.md` current for module-level behavior and operational details.

### Deploy the dashboard from Telegram

Select `/repo_use ai-dashboard`, then run `/deploy main`. The bot queues an
independent job with `/usr/local/sbin/ai-deploy submit deploy`; it reports the
operation ID immediately. Check completion, failures, and current/previous
verified releases with `/deployments` in the Ops bot. The manager owns restarts,
including coding-agent self deployment. Install the central manager from the
Ops repository before using this command. The dashboard has no shared-core
submodule, so it is excluded from `/core update`.


### Dashboard provider limits

The coding snapshot includes a secret-free `limits` field. A background task
reads Codex account quota through `codex app-server` and
`account/rateLimits/read` every five minutes, using this server's existing
ChatGPT login. No model inference is performed. Named quota buckets take
precedence over the legacy single bucket; absent windows are unavailable.
Checks time out after 20 seconds and do not delay the snapshot heartbeat.

Claude API headers are captured from real planning calls, persisted in
`CLAUDE_API_LIMITS_FILE` (default `/var/lib/ai-coding-agent/claude-api-limits.json`),
and displayed with their age. `/limits claude` reads that cache without making
an API request. No limits probes consume tokens. Claude Code readings come
from its status line and remain separate from API limits.

Model switches verify the candidate, save it, then schedule an independent
restart of `AGENT_SERVICE_NAME` (default `ai-coding-agent`). Restart scheduling
failures are reported rather than claiming success. The dashboard shows the
last model-switch result permanently beneath the Models list.

### Claude Code subscription limits

A separate `limits.claude_code` snapshot entry reads passive status-line data.
It contains only 5-hour/weekly quota remaining and resets, never session IDs,
credentials, prompts or costs. Expired windows are removed without assuming
the quota reset to full. Missing readings explain that an interactive session
is required. This feature does not poll private OAuth endpoints or run inference.

After deploying, enable capture once under the account that runs Claude Code:

```bash
cd /opt/ai-coding-agent
/opt/ai_coding_venv/bin/python -m ai_agent.claude_statusline_setup
```

The installer preserves the existing status-line command and all other settings.
Interactive Claude Code sessions then publish readings after their first API
response (when the subscription supplies quota data). The Telegram bot uses
headless `claude -p`; those runs do not refresh status-line percentages.
The dashboard labels reading age and never presents missing data as zero usage.

`CLAUDE_CODE_LIMITS_FILE` defaults to `claude-code-limits.json` alongside the
coding snapshot. Use the same path for the interactive session and service
if overriding it; the capture file is owner-only.

Core pin updates are prepared and pushed from a temporary checkout of remote main.
The running repository is left unchanged until the deployment manager installs and verifies the update.

### Coding runs that need server evidence or an operational action

For the four managed agent repositories, the orchestrator calls the Ops-owned
`/usr/local/sbin/ai-diagnostics <service>` before planning and implementation.
Install it using the Ops README. The coding model receives bounded, redacted
read-only evidence in its prompt, without opening live env files or changing its
CLI permission mode. Other repositories receive no server diagnostics.

A run that makes no repository changes can return a structured `blocked` or
`ops_required` report with `summary`, `evidence` and `next_step`. Unstructured
no-change reports default to Blocked. Telegram reports the outcome; the snapshot
and persistent state retain it, and linked tasks remain open and retryable.
These outcomes skip commit, PR creation and CI. They never complete a linked todo.
Use the existing Ops bot/dashboard controls for reviewed server actions. Actions
outside those controls still require an administrator; arbitrary shell execution
is not exposed to the model. The report is a proposal, not proof of a repair.

## Autonomous worker (`/worker`)

When switched on (dashboard Tasks window, or `/worker on`), the worker checks
every minute and, while the agent is idle, takes the oldest **To do** task:
it plans it, waits for **your** approval, then runs it to a pull request. It
never merges or deploys, and handles one task at a time.

Budget, changeable in the dashboard or with `/worker budget <runs> <reserve%>`:
implementation runs per 24 h (0 = no cap, default 3) and the share of a Codex
limit window it must leave for you (default 30%). Claude Code's limits cannot
be read outside an interactive session, so they are not checked from readings.

With tool switching on (the default; `/worker tools auto|fixed` or the
dashboard), the worker uses your chosen planner or implementer while it has
room and the other one otherwise, for that plan or run only; your setting is
not changed. A run that fails with a usage-limit error marks its tool out
until the reset time in the error (an hour if none is given), puts the task
back to To do, and does not count as a failure. A task the
worker fails is not picked again until you start it; after three failures in
a row the worker switches itself off and says so in the chat.

### Autopilot tasks

A task marked **Autopilot** (when you make it, or later in Tasks) doesn't wait
for you: with the worker on, it is planned, the worker approves its plan and
runs it to a pull request. The pull request (and CI) is your review; nothing is
merged or deployed by itself. Unmarked tasks still wait for your approval. If
autopilot can't approve a plan (for example it belongs to another
repository), the task waits for you and says so. The daily run cap and the
limits reserve still apply.

### Code reviews by the worker

When the worker is idle it reviews one registered repository a day, in turn,
read-only (Codex in a read-only sandbox, or Claude Code in plan mode, run in
that repository), with its `AGENTS.md` and the coding rules. It asks for at
most N proposals (Worker card **Review proposals**, or `/worker reviews <0-10>`;
0 switches reviews off; default 3), each a bug, an improvement or a feature
with its files. They arrive as Proposed tasks you accept or dismiss; one
already proposed or dismissed is not proposed again for 30 days. It uses the
planner tool, keeps the limits reserve and switches tools like other worker
runs. `/worker review` (or **Review a repo now**) reviews the next one at once.

### Todos tagged #auto

With the worker on, every 10 minutes it reads the PM todo lists (read-only,
through the PM agent's bridge `/usr/local/sbin/ai-pm-todos`, the one the
dashboard uses) and turns each open todo whose text has `#auto` into an
Autopilot task, once (a task you remove is not made again). Add
`#<repository>` (for example `#ai-dashboard`) to say where it is planned;
otherwise a repository named like the todo list, else the active project.
If the plan belongs to other repositories, autopilot moves or splits it
(`/split`) instead of waiting for you. The todo is closed when the task is
deployed, as for any task made from a todo.

### Retrying a failed run

When a task's run stops without a pull request (a tool ran out of its usage
limit, a crash), the task keeps the approved run: **Retry with Codex** or
**Retry with Claude** in Tasks runs the same plan again on a fresh branch from
the base, without planning it again. A run that started before this version
kept nothing to retry; start planning such a task again.

### Worker stats

The worker keeps the last 7 days of its finished steps: plans made, runs that
opened a pull request, failures and usage-limit stops, with the tool that did
each. The dashboard's Worker card shows them per day with the latest five.

### Tasks from the server logs

Once a day, when it has nothing to do, the worker reads the last 24 hours of
error-level journal lines of the four agent services, groups lines that differ
only in numbers, ids and times, and proposes a task for the three most
frequent new errors ("Fix error in ai-pm-agent: …", with example lines). No AI
is used for this; the planner looks at the error once you accept the proposal.
Proposals never start by themselves: accept one in Tasks (it becomes To do) or
dismiss it, and that error is not proposed again for 30 days. `/worker scan`
(or **Scan logs now** in the dashboard) scans at once.

### Task images

Send a photo or image document to the owner bot with the task description as its
caption. An album becomes one task after two seconds without another image;
check the reported image count because Telegram provides no album total.
Missing captions, invalid files and oversized batches are rejected. Tasks can
also be created through inbox uploads; the dashboard upload UI is **part 2**.

Attachments support decoded, single-frame PNG, JPEG and WebP, at most 10,000,000
bytes each, 16 million pixels, 8000 pixels per side and five images per task.
Pillow is required for decoding (`requirements.txt`). Defaults:

- `IMAGE_DIR=/var/lib/ai-coding-agent/images` (private, outside git)
- `IMAGE_STORAGE_CAP_BYTES=5GB` (plain bytes or a unit such as `500MiB`; the
  default is decimal 5 GB)
- `IMAGE_MAX_BYTES=10MB`
- `IMAGE_MAX_PER_TASK=5`

The storage cap is enforced at startup and before every atomic save. Eviction
always removes the oldest stored images, including queued and active tasks.
Tasks retain opaque references and report missing images when run; missing
attachments alone do not fail a task. Removal and pruning clean unshared files.
Disk-full errors reject uploads; the bot never silently reduces the configured
cap. No disk capacity has been established by this change. Any server storage,
permissions or quota change must go through Ops.

Codex planners/implementers receive `--image` attachments, Claude API planners
receive base64 image blocks, and Claude Code implementers receive native blocks
on stream-json stdin. Provider selection and fallback remain available. Image
content goes only to the selected provider; snapshots contain aggregate usage
and task image counts, with no bytes or local paths. See the
[upload contract](docs/task_images.md) before implementing dashboard part 2.
