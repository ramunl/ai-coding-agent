# projects.py

Multi-project support. A "project" bundles the four settings that must always
move together:

| Field | Example |
|---|---|
| repo_path | /opt/projects/channel-cast |
| github_repository | ramunl/channel-cast |
| base_branch | main |
| rules_project | channel-cast → ai-rules/projects/channel-cast/ |

## Why this module exists

These were four independent module-level constants (`REPO_PATH`,
`GITHUB_REPOSITORY`, `GITHUB_BASE_BRANCH`, `RULES_PROJECT_NAME`). Constants
bind at **import time**, so nothing could change them at runtime. The worst
case was `shell.run(args, cwd=REPO_PATH)` — a default argument evaluated once
when the module loaded, forever.

Everything now resolves through `active_project()` **at call time**.

## Registry

State lives in `PROJECTS_FILE` (default `/etc/ai-agent-projects.json`):

```json
{
  "active": "channel-cast",
  "projects": {
    "channel-cast": {
      "repo_path": "/opt/projects/channel-cast",
      "github_repository": "ramunl/channel-cast",
      "base_branch": "main",
      "rules_project": "channel-cast"
    }
  }
}
```

The active project is persisted, so it survives a service restart.

## Backward compatibility

If the file is missing **or unreadable**, the registry is synthesized from the
old env vars (`REPO_PATH`, `GITHUB_REPOSITORY`, `GITHUB_BASE_BRANCH`,
`RULES_PROJECT_NAME`). A single-project install therefore keeps working with
zero configuration, and a corrupt file degrades to the env project rather than
taking the agent down.

## Commands

| Command | Description |
|---|---|
| /repo_list | List projects; active marked with `*` |
| /repo_add \<owner/repo\> [path] | Register, and clone if absent |
| /repo_use \<name\> | Switch the active project |
| /repo_remove \<name\> | Unregister (cannot remove the last one) |

`/repo_add` accepts `owner/repo`, an SSH remote, or an HTTPS URL. When no path
is given the repo is cloned into `PROJECTS_ROOT/<name>` (default
`/opt/projects`).

## Safety: knowing which repo is active

The active project is shown in three places, in increasing order of
importance:

1. `/start` header — informational
2. Bot display name via `setMyName` — **cosmetic only**. Telegram rate-limits
   profile changes and clients cache the name, so it is eventually consistent
   at best. Failures are logged and ignored. Never rely on it.
3. **The rendered plan** — `Project: <name> (<owner/repo>)` appears directly
   above the branch, so `/approve` cannot be given without seeing the target.
   This is the real interlock.

## Note on add_project

`add_project()` returns `(project, needs_clone)` rather than cloning itself.
This keeps the module free of any shell dependency, so the import graph stays
acyclic: `config → projects → shell`.
