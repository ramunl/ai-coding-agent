# Verification-only task completion

An ordinary implementation that makes no changes remains Blocked. A model's
success report never proves that checks ran and never authorizes completion.

For a task whose full acceptance criteria are running the existing repository
baseline, approve a structured plan with:

- `verification_profile`: `<registered-repository>:baseline-v1`
- `files`: `[]`
- One execution repository, matching the task and profile.
- A matching `repository_accounting` entry with `role: verify`.

For dashboard page tests only, select `ai-dashboard:page-tests-v1`. It runs
`npm test` only, with the same disposable worktree, existing JavaScript
dependencies, receipt and cleanup requirements. It does not require Ruff or Python checks.

Verification-only plans with an empty profile are rejected before approval and
show an actionable blocker in the plan outline. They never silently dispatch to
an editing model. This validation also applies to old pending approvals.

Supported repositories are ai-coding-agent, ai-pm-agent, ai-ops-agent and
ai-dashboard. The plan shows the exact fixed commands before approval.
Profiles run Ruff check, Ruff format --check and the full Python pytest suite.
The dashboard also runs npm test. Coding's Ruff paths include agent.py and its
statusline script, matching its documented baseline. Arbitrary/manual verification needs explicit execution support or a revised,
reviewable set of automated checks. Feature edits use implementation plans.
Changing an existing profile's commands requires a new profile version.

The application obtains the repository execution lease, verifies the remote,
updates the clean base with a fast-forward pull, and creates a disposable detached
worktree with recursive submodules. Python baseline checks prepare a separate
temporary virtual environment from that checkout's requirements-dev.txt, including
its referenced runtime requirements. Python, pytest and Ruff all run from this
environment, rather than borrowing the Coding controller's packages. Dependency
installation and consistency checks must succeed before test execution. Missing
requirements, unavailable venv/pip, package access or dependency conflicts produce
an explicit setup failure and never a successful verification receipt.

The environment is outside the Git checkout and removed on success, failure or
cancellation. It does not modify any production bot environment. Preparation uses
the configured worker identity, supervised process groups, a bounded output tail
and a 600-second timeout per setup command. The approval outline shows that test
dependencies will be prepared; package downloads may require network access.

JavaScript-only profiles skip Python setup entirely. Dashboard page tests continue
to use the existing node_modules directory. Checks use the configured worker
identity when isolation is enabled, bounded output and supervised cancellation.
No model, commit, push or PR is required. The worktree is removed afterwards.

Completion requires exit 0 from every command, unchanged HEAD and clean tracked
and untracked files, successful cleanup, and a durably archived application
receipt. The receipt binds the profile, commands, exit codes, task, attempt and
exact tested commit. Failure or interrupted execution does not count as success.
Only the specified checks are proven; this is not a claim of feature acceptance.

A `verified_in_checkout` dependency requires this receipt and freshly checks the
repository remote, exact HEAD and clean checkout. A later commit requires rerunning
verification; it cannot inherit the earlier test results. PR and deployment
policies retain their existing requirements.

## Existing blocked verification tasks

Deploy this change, then plan the blocked verification task again using the
profile above and approve the new plan. Do not mark its previous model report as
completed and do not split the same task again. For an unstarted dependent task,
use **Check prerequisites and start** after verification finishes. This explicit
owner action can review and migrate an old merged-PR edge to the new verified
receipt, recording the policy and reference change. Approved or already running
consumers are never rewritten. Deployment requirements are never weakened.

Claude preflight uses `claude --version` only. It does not invoke `claude config
stat`: that unsupported command can be interpreted as a model prompt. Existing
credential observers and actual provider failures retain their authentication
semantics; a successful version check does not claim a valid login.
