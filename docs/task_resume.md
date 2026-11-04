# Resume saved work and Start fresh

A failed implementation with a saved approved run offers two separate actions:

- `resume_task {task, tool}` restores that task's saved branch, staged changes,
  unstaged changes and untracked files, then starts a fresh provider invocation
  with the original approved prompt and an instruction to inspect remaining work.
- `retry_task {task, tool}` is explicitly **Start fresh**. It protects previous
  branch tips and dirty work in immutable recovery refs, then resets to the latest
  base branch. It never silently claims to continue old work.

This is repository checkpoint continuation, not provider conversation/session
resumption. Model reasoning history is not restored. Old recovery refs without a
validated task manifest are never guessed or applied automatically.

## Storage and safety

Private `.git/ai-agent-checkpoints/<hashed-task-id>.json` manifests bind the task,
checkout path, common Git directory, remote, branch, starting commit and generated
recovery refs. Object IDs validate immutable recovery refs before restore. Files
are atomically replaced and kept separate from task-list pruning. Saved recovery
refs remain available after repeated failures and clean restarts.

The repository lease excludes concurrent runs. Provider commands run in a new
process group. Timeout terminates the entire group and verifies descendants have
stopped before recovery; persisted `.git/ai-agent-provider.json` fails closed on
an unknown/live process after restart. Async cancellation waits for the worker
thread to finish rather than releasing the lease while it can still edit files.
It does not provide an immediate active-run stop button. Normal shutdown can wait
until the configured provider timeout; forced shutdown may need Ops intervention
if a live provider marker remains.

Resume refuses empty, missing, corrupt, changed-repository, moved-branch or
unverified-process checkpoints. After a service crash, the next fresh run first saves the original bound
checkout into its task manifest before switching branches. This preserves
resume ownership even when another task runs before the original is resumed.
Capture requires the checkout still to be on the recorded original branch. Legacy attempts have
no manifest and require manual inspection; no unsafe reconstruction occurs.

After PR publication, use the existing PR/CI repair workflow. Both task Resume
and Start fresh reject published checkpoints to avoid duplicate PRs. Interrupted
CI repairs retain a task-bound checkpoint and recovery refs, with an explicit
manual-recovery reason; restoring those edits is not yet part of the task resume
flow. A pre-publication push/PR failure remains resumable. A journaled PR-creation
failure is reconciled when GitHub successfully confirms no PR for the saved
branch and the checkout has no active writer. Uncertain requests and failed
GitHub lookups remain blocked. Rejected dashboard actions are recorded in both
action results and the service journal.

Usage-limit failures pause implementation tasks for an explicit owner choice;
the worker no longer sends them back to To do for an automatic clean run.
Planning-only usage limits may still retry planning later.

## Checks

Real-Git tests cover partial commits, separate staged/unstaged edits, untracked
files, repeated failures, interrupted-checkout recovery, start-fresh preservation,
invalid identities/refs, moved branches and empty checkpoints. Process tests use
local child writers and prove they cannot write after timeout or leader exit.

Task PR tracking binds modern archived attempts to their recorded commit SHA
and PR URL when available. Reusing a branch name cannot attach an older merged
PR to a later failed attempt. Missing archives remain unresolved rather than
marking the task complete.
