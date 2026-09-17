# Telegram bot architecture

`ai_agent.telegram_bot.build_application()` wires commands, callbacks, free-text
replies, startup hooks, and error handling. `agent.py` validates configuration,
configures logging, and starts polling. Command logic lives in `ai_agent/bot`.

## Module ownership

| Module | Responsibility |
| --- | --- |
| `constants.py` | Autocomplete command metadata and fleet deployment targets |
| `transport.py` | Owner authorization, redacted chunked replies, argument prompts, errors |
| `state.py` | Pending plans, FIFO task data, provider preferences, execution snapshots |
| `menus.py` | Concise help, full reference, and command button grids |
| `planning.py` | Plan creation, revision, approval, and bugfix clarification |
| `execution.py` | Queue execution, pull request repair, and cleanup |
| `ci_monitor.py` | CI polling, repair prompts, and pull request selection |
| `inspection.py` | Status, queue, logs, diffs, verbosity, and cancellation |
| `repositories.py` | Project registration, selection, branches, and pulling changes |
| `providers.py` | Planner/implementation provider choices, limits, and model settings |
| `maintenance.py` | Versions, deployment, unit tests, and shared-core operations |

The command adapters use existing services such as `workflow.py`, `planner.py`,
`projects.py`, and `ci.py`. Shared transport and state helpers never import the
application wiring module. Callback dispatch remains in the wiring layer so
command modules do not need to import each other to route a command.

## Commands and callbacks

`command_handlers()` is the source of handler routing for both typed commands and
command buttons. `BOT_COMMANDS` supplies autocomplete labels and menu buttons.
Help and `/more` use standard Telegram messages with a three-column button grid.
Argument-free commands execute immediately. Commands needing choices show a grid;
free-text commands use a force-reply prompt. A reply is accepted only when it
matches the recorded prompt message ID. `/cancel` clears that prompt.

Every command and callback checks owner authorization. `reply_chunks()` redacts
configured secrets and splits plain-text output into at most 3,900 characters.
Repository listings retain their existing structured rich-message payloads.

## Planning and execution state

`context.user_data` retains the existing keys and dictionary shapes:

- `pending_plan`: the current `PlanState`, with revision and approval state.
- `pending_implementation`: change, prompt, branch, commit type, and display labels.
- `pending_bugfix_clarification`: bug report and branch source awaiting answers.
- `argument_prompt`: prompt message ID and command awaiting a text reply.
- `task_queue` and `next_task_id`: FIFO tasks and their identifiers.
- `queue_runner_active` and `active_execution`: runner ownership and progress.
- `last_execution`: an `ExecutionState` containing output, diff, PR URL, and test status.
- `planning_agent`, `implementation_agent`, and `verbosity`: user preferences.

`/plan` creates a pending plan; `/discuss` revises it; `/approve` marks the revision
ready. `/confirm` snapshots approved work and the selected implementation provider
into the queue. The runner processes tasks in order, implementing each branch,
pushing it, opening its pull request, and polling CI. Its `finally` blocks clear
execution state, restore the base branch, and release the runner flag on failure.
An exception still stops the current drain; later queued tasks remain pending.

`/fixpr` checks that the requested PR is open and belongs to the active repository.
It rejects fork branches and uses the existing PR branch for repairs.

## Shared CI repair sequence

Queued work and `/fixpr` both call `_repair_failed_ci()`. An immutable
`_RepairRequest` supplies the branch, PR, provider, prompt builder, repair operation,
and commit message. The loop repairs only a failed result, respects
`CI_FIX_ATTEMPTS`, captures updated execution output, and polls each pushed commit.

`watch_ci()` reports changed summaries and stops on success, failure, or
`CI_TIMEOUT_SECONDS`. `_report_execution()` updates the captured test status using
`dataclasses.replace` and renders the completion message. Checking an already
passing PR does not overwrite output from an earlier implementation.

## Maintenance and validation

The shared-core path is resolved from the repository root, independently of the
new package depth. Fleet scripts and aliases live in `constants.py`; environment
configuration remains in `ai_agent/config.py`. `/version` has a single registered
handler backed by the shared version implementation.

Bot behavior tests are split into `tests/test_bot_*.py`; registration and dispatch
integration tests remain in `tests/test_telegram_bot.py`. `tests/bot_fixtures.py`
isolates mocked Telegram/provider modules between tests. Mock services where the
owning command module imports them.

Run the checks documented in the README before committing. Ruff excludes the
pinned `ai_agent_common` submodule from formatting, while pytest includes its
regression tests.
