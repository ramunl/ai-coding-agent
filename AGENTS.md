# ai-coding-agent

A Telegram bot that plans and implements features and bugfixes in the owner's
registered repositories, then opens pull requests. One owner (`YOUR_CHAT_ID`);
runs as a systemd service on a small (1 GB) Ubuntu VPS next to the PM and Ops
bots and the `ai-dashboard` Mini App.

## How it works

- `/plan`, `/implement`, `/bugfix` → a planner (Codex CLI or the Claude API)
  writes a JSON plan → the owner approves → an implementer (Codex CLI or
  Claude Code) edits a branch in the active project → tests → push → PR → CI
  is watched and failures repaired.
- Tasks (`tasks.py`) follow work from a todo to Deployed; the worker
  (`worker.py`, `bot/worker_loop.py`) takes To do tasks by itself within a
  budget, proposes tasks from server logs (`log_scan.py`) and takes todos
  tagged #auto as Autopilot tasks (`auto_todos.py`); it also reviews one
  repository a day for proposals (`code_review.py`).
- The dashboard never calls the bot directly: it reads `snapshot.json` and
  drops request files into the inbox (`inbox.py`, a fixed allow-list of
  actions with validated arguments).

## Layout

- `agent.py` – entry point. `ai_agent/telegram_bot.py` – builds the app.
- `ai_agent/bot/` – Telegram handlers by area (planning, execution, menus,
  maintenance, …), persistence, the snapshot publisher, worker and inbox glue.
- `ai_agent/` – logic without Telegram: `planner.py` (prompts),
  `workflow.py` (implementation runs, branches), `plan_state.py`, `tasks.py`,
  `projects.py` (registry in `/etc/ai-agent-projects.json`), `rules.py`
  (coding rules from the `ai-rules` repo), `project_brief.py` (this file in
  prompts), `ecosystem.py` (the map of all repositories), `task_split.py`
  (a multi-repository plan as one task per repository), provider limits and
  models.
- `ai_agent_common/` – git submodule shared with the other bots
  (auth, versions). Changing it needs `/core release` and bot updates.
- `deploy/` – systemd unit and update script. `docs/` – per-module notes.

## Commands

- Tests: `python3 -m pytest -q` (unit tests only; no network, no real
  `/etc` or `/var/lib` – use temp dirs and env vars).
- Lint: `ruff check agent.py ai_agent tests` and `ruff format --check …`
  (ruff 0.16.8, line length 88). Format only the files you touch.
- CI (`.github/workflows/test.yml`) runs both; merge to main deploys.

## Conventions and traps

- Settings are read once at import from env (`ai_agent/config.py`); tests set
  env before importing and pop modules they reload. Don't import config lazily.
- Tests fake `telegram`/`anthropic` modules; keep heavy imports out of light
  modules and guard optional ones.
- Every `user_data` key must be classified in `bot/state.py`
  (persistent vs transient); a test enforces it.
- Dashboard-facing actions must be added both to `inbox.py` and to
  `ai-dashboard`'s `agent_actions.py`.
- Never print or log secrets; use `redact_sensitive`.
- Keep files small and split by responsibility (see the `ai-rules` file-size
  rules); one feature per PR.
