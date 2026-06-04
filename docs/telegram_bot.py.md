# telegram_bot.py

**Purpose**: Main Telegram bot interface for the AI agent, handling user commands and orchestrating the workflow.

## Overview
Provides command handlers for a Telegram bot that allows users to request features, report bugs, confirm implementations, and monitor CI status.

## Core Components

### Authorization

#### `is_authorized(update: Update) -> bool`
Checks if a message comes from the authorized chat.
- Compares `update.effective_chat.id` with `CHAT_ID` config
- Returns boolean

#### `require_authorized(update: Update) -> bool`
Requires authorization and logs warnings if unauthorized.
- Returns False if unauthorized (with warning log)
- Returns True if authorized

### Response Handling

#### `reply_chunks(update: Update, text: str) -> Coroutine`
Sends response text, split into Telegram-compatible chunks.

**Features**:
- Redacts sensitive information (tokens, keys)
- Splits at 3900 chars (Telegram limit is 4096)
- Sends as multiple messages if needed
- Shows "(no output)" if text is empty

## Command Handlers

All commands are async and require authorization via `require_authorized`.

### Help Commands

#### `/start` or `/help`
Displays available commands and their usage.

Commands that need arguments are shown without a leading slash in help text so Telegram does not send
an incomplete command when the user taps it. Users still type the slash when running them.

**Shows**:
- plan \<feature\> - Plan only, no implementation
- implement \<feature\> - Plan and wait for /confirm
- bugfix \<bug\> - Clarify if needed, then wait for /confirm
- answer \<details\> - Answer pending clarification
- /confirm - Add pending work to the FIFO queue and run queued tasks
- /queue - Show running task and pending FIFO queue
- agent codex|claude - Choose the AI used for implementation and CI repair
- cancel \[task-id\] - Discard pending work or remove a queued task
- ci \<pr-number\> - Show CI status for PR
- /limits - Show Claude API limits
- /codex - Show Codex CLI status
- /test - Run agent unit tests
- /branches - List git branches
- /status - Show git status
- logs \[lines\] - Show recent service logs

### Repository Commands

#### `/branches`
Lists all git branches (local and remote).
- Runs: `git branch -a`

#### `/status`
Shows git status.
- Runs: `git status`

#### `/logs [lines]`
Shows recent service logs.
- Default: 60 lines
- Max: 120 lines (configurable)
- Runs: `journalctl -u ai-agent.service -n {lines} --no-pager`

### Monitoring Commands

#### `/limits`
Shows current Claude API rate limits.
- Calls: `get_anthropic_limits()`

#### `/codex`
Shows Codex CLI version and login status.
- Calls: `get_codex_status()`

#### `/test`
Runs agent unit tests.
- Calls: `run_unit_tests()`

#### `/ci <pr-number>`
Checks CI status for a specific PR.
- Gets PR head SHA from GitHub
- Calls: `evaluate_ci(head_sha)`
- Shows current workflow status

### Feature/Bug Commands

#### `/plan <feature>`
Generates an implementation plan without executing it.
- Calls: `plan_feature(feature)`
- Returns: Branch name, files to modify, steps, Codex prompt

#### `/implement <feature>`
Plans a feature and stores for later confirmation.
- Calls: `plan_feature(feature)`
- Generates branch name
- Stores in `context.user_data["pending_implementation"]` with:
  - change, codex_prompt, branch_name
  - commit_type: "feat"
  - pr_body_label: "Plan"
  - confirmation_label: "implementation"
- Waits for `/confirm` or `/cancel`

#### `/bugfix <bug>`
Triages a bug report, asking clarification questions if needed.
- Calls: `assess_bugfix_report(bug)`
- If questions needed:
  - Stores in `pending_bugfix_clarification`
  - Waits for `/answer`
- If ready:
  - Proceeds to `prepare_bugfix()`

#### `/answer <details>`
Provides answers to bugfix clarification questions.
- Requires pending `bugfix_clarification` context
- Combines original bug with user answers
- Re-assesses with questions
- If still questions: waits for more answers
- If ready: proceeds to `prepare_bugfix()`

### Implementation Commands

#### `prepare_bugfix(update, context, bug, branch_source)`
Internal function - prepares bugfix for confirmation.
- Calls: `build_bugfix_prompt(bug)` and `slugify_branch_name(branch_source, "bugfix")`
- Stores in `pending_implementation` with:
  - commit_type: "fix"
  - pr_body_label: "Bug fix prompt"
  - confirmation_label: "bug fix"

#### `/confirm`
Enqueues the pending implementation (feature or bugfix) and drains queued work in FIFO order.

**Process**:
1. Requires `pending_implementation` context
2. Appends the pending task to `task_queue`
3. Returns immediately if another queue runner is already active
4. Otherwise drains `task_queue` from oldest to newest
5. For each task, ensures GitHub is configured
6. Runs Codex: `implement(codex_prompt, branch_name)` (runs: `codex {prompt}`)
7. Commits and pushes: `push(branch_name, change, commit_type)`
8. Creates PR: `create_pull_request(branch_name, change, codex_prompt, commit_type, pr_body_label)`
9. Watches CI: `watch_ci(head_sha)`
10. If CI fails, repairs and pushes the branch up to `CI_FIX_ATTEMPTS`, polling each repair commit
11. Sends a passing completion or a failed completion when the final polled CI result is failed

**Error Handling**:
- GitHub configuration errors caught early
- Codex errors from implementation failure
- Git errors from push failure
- PR creation errors
- Exhausted CI repair attempts are reported as a failed implementation, not a successful completion

#### `/cancel`
Discards pending implementation or bugfix clarification, or removes a queued task by ID.
- Without arguments, clears `pending_implementation`, `pending_plan`, and `pending_bugfix_clarification`
- With `/cancel <task-id>`, removes a queued task that has not started
- Running tasks are not cancelled

#### `/queue`
Shows the currently running task and pending FIFO tasks.
- Uses `active_execution` for the running task
- Uses `task_queue` for pending tasks

### CI Polling

#### `watch_ci(update, head_sha)`
Polls CI status until completion or timeout.

**Behavior**:
- Polls every `CI_POLL_INTERVAL_SECONDS` (default 30s)
- Timeout: `CI_TIMEOUT_SECONDS` (default 1800s = 30 min)
- Reports status changes:
  - "waiting" → "running" → "passed" or "failed"
- The task runner also repeats the final passed CI status before the implementation completion summary
- Exits when:
  - Status is "passed" or "failed"
  - Timeout reached

## Internal Helpers

#### `get_bugfix_questions(bug: str) -> str | None`
Wrapper that assesses a bug and extracts questions.
- Calls: `assess_bugfix_report(bug)` then `bugfix_questions()`

## Error Handling

#### `error_handler(update, context)`
Global error handler for Telegram updates.
- Logs all errors with traceback
- Sends error message to user if authorized
- Format: "Error:\n{error}"

## Application Setup

#### `build_application() -> Application`
Constructs and configures the Telegram bot application.

**Handlers Registered**:
- /start → start()
- /help → start()
- /plan → plan()
- /implement → implement_cmd()
- /bugfix → bugfix_cmd()
- /answer → answer()
- /confirm → confirm()
- /cancel → cancel()
- /ci → ci()
- /limits → limits()
- /codex → codex_status()
- /test → test()
- /branches → branches()
- /status → status()
- /logs → logs()
- Global error handler

**Returns**: Configured `Application` ready to run

## Configuration Dependencies

- `CHAT_ID`: Authorized chat ID
- `TELEGRAM_TOKEN`: Bot token
- `CI_POLL_INTERVAL_SECONDS`: Polling frequency
- `CI_TIMEOUT_SECONDS`: Max CI wait time
- `COMMAND_TIMEOUT_SECONDS`: Shell command timeout
- `MAX_TELEGRAM_MESSAGE_LENGTH`: 3900 chars
- `MAX_LOG_LINES`: 120 lines max
- `GITHUB_REPOSITORY`: For CI checks

## Usage Pattern

```python
from ai_agent.telegram_bot import build_application

app = build_application()
app.run_polling()  # Start bot
```

## Workflow State Management

Pending operations are stored in `context.user_data`:

**Implementation State**:
```python
context.user_data["pending_implementation"] = {
    "change": str,
    "codex_prompt": str,
    "branch_name": str,
    "commit_type": str,  # "feat" or "fix"
    "pr_body_label": str,
    "confirmation_label": str
}
```

**Bugfix Clarification State**:
```python
context.user_data["pending_bugfix_clarification"] = {
    "bug": str,
    "branch_source": str
}
```
