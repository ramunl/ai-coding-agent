# Dependency admission

Display stages (`pr`, `done`, etc.) remain unchanged. They are not evidence of
readiness. Unknown evidence blocks planning/implementation and is shown through
`waiting_for`. Display helpers do no evidence I/O. Starts and execution admission
refresh evidence even when a recent display decision says satisfied.

A dependency contains a stable task ID, owner/repository scope, optional immutable
attempt reference, policy and revision. Required code defaults to
`merged_in_checkout`. Split tasks capture each predecessor's own repository.
Legacy `after` and `dependency_ids` are parsed strictly; missing scope/attempts
block. Available handoffs are pinned before finished tasks leave `MAX_TASKS`.
Reopening invalidates retained consumers and queued task snapshots; the owner must
review/rebind those edges before continuing. No active-project fallback is used.

Policies:

* `reviewable_pr`: GitHub confirms the exact archived PR is open, explicitly not a
  draft, in the captured repository, with the exact archived attempt head. This
  does not assert passing CI or a review approval.
* `merged_in_checkout`: GitHub confirms a merged PR and its merge commit is an
  ancestor of HEAD in the predecessor's captured checkout. Squash merges use the
  merge commit, not the original feature head. If a verified merged prerequisite
  is absent locally, admission may fetch and fast-forward the registered base
  branch as described below. Insufficient shallow history remains unknown.
* `deployed`: additionally requires a healthy Ops deployment within 15 minutes,
  with the merge commit contained in the deployed commit. Rollbacks, recovery,
  active transactions, mismatched targets and checkout drift block.

PR facts and display decisions have a 60-second maximum age. At most eight edges
are admitted per task. Git calls time out after five seconds; exact PR reads use
10 seconds and 256 KiB. Worker passes refresh at most two tasks, ordered by oldest
check. Decisions are rechecked after async work and immediately before execution.

The Ops contract is the authoritative `/var/lib/ai-deploy/state.json` written by
`ai_ops_agent.deployment.model.Store` and `engine.Manager`: `targets[name]` has
`current.commit`, `current.verified_at`, `status`, `operation`, and `history`.
`verified_at` is UTC successful restart/health verification time, **not** the time
of collecting diagnostics. The latest history entry must be a healthy `deploy`
for the same operation. The fixed deployed checkout `/opt/<target>` must still
match the recorded commit and GitHub identity. Reads are capped at 256 KiB and
three seconds, reject symlinks/nonregular files, and never create/chmod Ops state.
Absent contract fields or denied access remain unknown. No diagnostics, model
prose, dashboard files, AI calls or server mutations supply deployment authority.

`dependency_overrides.approve_override` is an adapter API for a trusted owner
review, not a worker/model action. It requires owner Approval provenance and a
reason, writes an approved durable decision, and returns its pinned reference.
The decision binds the complete edge, including scope, attempt, policy and
revision. Changing any of those fields invalidates it. No new dashboard action or
automatic override is introduced. Review/rebinding must preserve this audit trail.


## Stale planner checkouts

The deployed service checkout and the planner's registered checkout are separate.
After a prerequisite merge, admission rechecks its archived attempt and GitHub PR
before attempting to update a stale planner checkout. The captured identity must
still match the registry and origin. The agent must be idle, with no pending plan,
implementation, queued execution or recovery. The checkout must be clean
(including untracked files), attached to its registered base branch, and able to
fast-forward. Feature branches, detached checkouts and local divergence are
preserved and remain blocked with an explanation.

Fetch is bounded to 30 seconds and runs off the event loop. After fetching, the
agent rechecks activity, HEAD, branch, origin and working tree. Final checks and
the fast-forward run without yielding to another bot handler. Synchronizations
are serialized. There is no reset, stash, cleanup, branch switch, automatic
history deepening or dependency override. The fetched branch must contain the
verified merge commit; all dependency evidence is evaluated again after an
update. Retries are bounded by the number of dependency edges. This applies to
manual starts and worker admission, even when the worker is off for manual work.

Dependency verification fingerprints the consumer's edge definitions and attempt
binding plus only its referenced predecessor tasks. Consumer display-stage changes,
unrelated task changes and task-list ordering do not invalidate the check.
Predecessor removal, stage/branch/attempt/handoff changes, and edge revisions still
invalidate in-flight verification. This does not bypass fresh PR, archive,
checkout ancestry or evidence-age checks.
