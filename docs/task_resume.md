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

For an unstarted To do task, the owner’s Check prerequisites and start action
can review a replacement predecessor attempt. It retains the same task, scope
and policy, verifies fresh PR and checkout evidence, then records the old and
new references in dependency_reviews. Worker polling never performs this review.
Approved plans and saved runs remain blocked for separate owner review.

An attempt may contain several commits from CI repairs. Dependency verification
checks the live PR head against those archived commits and still requires one
identity-bound PR; unrelated heads remain blocked.

## PR evidence and reconciliation

Every PR lookup is classified into one of four states — `absent` (GitHub
answered and there is no PR), `verified` (an exact archived PR still matches),
`mismatched` (a PR exists but does not match the archived attempt) or
`unavailable` (the lookup failed or was malformed). Only `absent` settles a
task; the other two leave it for the next poll or owner review. An archived PR
URL is re-checked exactly; branch searches only discover a candidate and never
bind a completion by themselves.

`reconcile_task` restores a verified **open** PR to `pr` and a verified
**merged** PR to `done` — never `deployed`. Before binding a late PR it
re-validates repository, task and attempt ownership. A repaired head (the live
PR head is a descendant of, but not equal to, the archived commit) is recorded
with explicit persisted provenance and leaves dependents blocked; only an exact
archived head may re-review unchanged dependents. Unrelated or unverifiable
heads are rejected with an actionable reason. Recovery refs, saved edits and
attempt history are preserved, and the restored `pr_url`/`pr_evidence` survive
a restart so tracking resumes.

Later DS03 reconciliation is intentionally **not** implemented here: the
repaired head must first be re-verified against fresh GitHub and archived
evidence and re-archived so strict dependency verification can pass, without
reopening or re-running the attempt.

The planner checks current and historical task branch names before displaying
or approving a plan. A first attempt whose proposed branch belongs to another
task receives a task-specific branch; reopened attempts retain their distinct
attempt branch. A modern run without its archived handoff cannot be marked
complete from a branch lookup. Failed replacement reviews return the current
verification reason instead of hiding it behind the previous invalidation.

The regression suite includes an ordered-task lifecycle: reopen a predecessor,
retain the blocked follower, record CI repair commits, reject an unmerged PR,
verify the merge and checkout, explicitly review the replacement, and repeat
the review without creating duplicate audit records.

## Stop implementation

The task list offers Stop implementation while a task-bound Claude or Codex
provider is running. Confirmation sends an opaque capability for that exact
process invocation; stale requests cannot stop a later attempt. The button
shows Stopping while provider shutdown and recovery finish. This stops the
provider process group, preserves edits using the existing checkpoint/recovery
flow, then marks the task Stopped only after verified cleanup. Autopilot is
switched off for the stopped task. Resume is offered only by the usual verified
checkpoint checks; a cleanup failure remains blocked by recovery safeguards.

The control is unavailable during planning, Git publication and passive CI
polling. It never kills arbitrary persisted PIDs, restarts the service, removes
a task, or closes its PR. After publication, saved CI edits still follow the
existing PR recovery rules.
