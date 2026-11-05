# Repository scope review

Feature plans and revisions use the same repository accounting contract for
Codex and Claude Code, including DeepSeek behind the Claude runner. The original
request, not its generated summary, determines the explicitly named registered
repositories that must be accounted for.

`repositories` contains implementation owners in dependency order. Multiple
owners retain the existing Split into tasks action. The additional
`repository_accounting` entries classify other requested repositories as:

- `verify`: explain the check and name it in a concrete plan step.
- `completed`: explain the exclusion and cite an evidence reference and an ISO
  date. These are planner-reported claims for owner review, not proof of a fresh
  live check. Future and invalid dates are rejected.
- `context`: explain why the repository needs no work in this plan.
- `unclear`: ownership or remaining work is unresolved; approval is blocked.
- `change`: the repository must also occur in the implementation owner list.

Missing, duplicate or conflicting accounting blocks approval and appears in
Telegram and the dashboard plan's risk details. Revise the plan before approving.
A multi-repository audit does not automatically create implementation tasks.
Already split tasks with the canonical `Assigned repository:` prefix validate
only their assigned part and cannot expand into sibling implementation work.
Direct multi-repository bugfix requests are routed to `/plan` for this review.

This adds no inference calls and does not rewrite or execute existing tasks.
Legacy plans remain readable; missing accounting must be supplied when an
explicitly multi-repository request is next approved. Names that the request does
not explicitly mention still rely on the planner's repository ownership analysis.
