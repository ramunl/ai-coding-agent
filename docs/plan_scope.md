# Planning scope advice

Before approval, Telegram and the dashboard show a scope warning when the
planner recommends smaller milestones. Each suggested milestone has a title
and an independent acceptance check. This applies within a single repository;
`/split` still only creates one task per repository and is not used here.

The owner can use `/discuss` (dashboard: Revise) to request only one milestone,
then review and approve the revised plan. The owner may also approve the full
scope. Suggestions never create tasks or execute work automatically.

The feature-plan schema accepts optional `scope_assessment` advice. Existing
saved plans remain readable. If advice is absent or malformed, a deterministic
read-only heuristic flags eight files, seven steps, or four steps spanning at
least three lifecycle concerns (storage, recovery, cancellation, user-facing
integration). It groups the existing steps into suggested checkpoints. This
heuristic can miss complexity: it does not inspect code or predict duration.
Provider advice is the primary assessment; no warning is a completion guarantee.

Advice is bounded and redacted before rendering. It does not claim an accurate
hour estimate or know how much work fits a provider session. An approved plan
still needs implementation tests and required compilation checks.

Bugfix triage keeps readiness separate from size: a ready report can still have
scope advice. Optional files/steps/risks and scope advice are parsed from the
same assessment. Legacy ready/questions responses remain supported. For a
broad ready fix, the warning appears before `/confirm`, and is published as
`pending_scope_assessment` for the dashboard. This does not create or approve a
feature plan. Narrow the fix with `/cancel`, then `/plan` for one milestone, or
confirm the original fix. Bugfix commit and execution semantics remain intact.

Milestones may specify a registered `repository` alongside `title` and
`acceptance`. Repository ownership is resolved for the whole split before any
task is created. Legacy titles beginning with a registered repository name are
also supported. Cross-repository verification milestones must have explicit
owners; ambiguous ownership is rejected instead of defaulting to the active UI
project. Each child and its dependency edge capture the actual repository.

An owner can move a wrongly assigned, unstarted split child to the plan's single
registered repository using the existing Move action. The task ID, incoming
links, outgoing links, group, todo and autopilot preference survive. Its old
plan is discarded and it returns to To do for planning in the corrected
repository. Execution history or a dependent that has already started blocks
this correction. Merely switching the active project does not move a task or
make an old approval valid. Approval refusals are returned as failed dashboard
actions (and logged), rather than successful actions carrying a refusal message.
