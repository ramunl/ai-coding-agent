# inbox.py and actions.py

How the dashboard changes the coding agent's setup without touching its state.

## Flow

1. The dashboard checks a request and writes it as one file into
   `AGENT_INBOX_DIR` (default `/var/lib/ai-coding-agent/inbox`).
2. `inbox.py` looks every 2 s, claims each file by renaming it (so it can
   never run twice), validates it again, and runs it through `actions.py`.
3. The result goes to `AGENT_ACTION_RESULTS_FILE`
   (default `/var/lib/ai-coding-agent/action-results.json`, last 20), which the
   snapshot publishes as `setup.actions`. A file, not memory, so the result of
   a model switch survives the restart it causes.

## Actions

| Action | Arguments | Same as |
|---|---|---|
| `use_project` | `name` | `/repo_use`; refused while a task runs or is queued |
| `add_repository` | `repository` (`owner/repo`) | `/repo_add`, clones if needed |
| `set_planner` | `value`: `codex` or `claude` | `/planner` |
| `set_implementer` | `value`: `codex` or `claude` | `/agent` |
| `switch_model` | `tool`: `claude`, `model` | `/model claude set`; verifies, saves, restarts |

Anything else is rejected: unknown actions, extra or missing arguments,
values outside the patterns, and ids that are not plain hex (ids become file
names). A request older than 2 minutes is reported as `expired`, not run.

`actions.py` is the single implementation: the Telegram commands, their
button taps, and the inbox all call it, so `/repo_use` gets the same busy
guard as the dashboard.

Results have `status` `running`, `done`, `failed`, `expired`, or `rejected`.
State changed by the inbox is marked for persistence explicitly, because
python-telegram-bot only saves users it just handled an update for.

## Setup in the snapshot

`bot/setup_view.py` publishes what the dashboard's pickers need: projects
(with the active one), planner and implementer with their options, each AI
tool's model (only manageable tools get `choices`; the Claude list is fetched
every 30 minutes), the busy reason, and the action results.
