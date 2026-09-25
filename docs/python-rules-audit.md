# Python rules review

Reviewed `agent.py` and `ai_agent/` against
`/opt/ai-rules/global/python.md` on 2026-09-25. This review covers the coding
agent; other agents and the independently versioned shared core are outside
this change.

## Findings and changes

- Formatting and imports already passed existing checks. Added enforcement of
  the 88-character limit, including strings the formatter cannot wrap itself.
  Message and prompt text is preserved.
- Added missing public module, class, method, and function docstrings. Filled
  the HTML parser and Anthropic request type-annotation gaps.
- Split model inspection, listing, and switching into separate handlers.
- Split GitHub reference fetching by reference type; expected I/O, decoding,
  and API failures remain visible in context. Unexpected programming errors
  now propagate instead of being mislabeled as failed network requests.
- Extracted source-file discovery and bounded matching from planning. Unreadable
  source files are logged and skipped, and scanning stops when both output
  limits are filled.
- Consolidated the duplicated implementation-result capture used by feature
  implementation and both repair paths.
- Separated queued implementation publishing and PR validation from CI repair
  orchestration, retaining cleanup in the outer `finally` blocks.
- Moved annotation and public-docstring checks into the shared Ruff
  configuration so local checks and CI enforce them across production code.

## Judgments and limits

No production module reaches the guide's 500-line warning threshold. The two
help-menu functions exceed the approximate function-length target because they
contain declarative message blocks; splitting that copy would not clarify the
control flow. Other execution and CI orchestration functions were reviewed for
natural boundaries rather than enforcing an arbitrary line-count cutoff.

Broad catches at background-publisher, shutdown, and optional-notification
boundaries retain their logging and resilience guarantees. Atomic-write cleanup
re-raises the original failure after removing the temporary file.

Existing dataclasses already cover the principal data containers. This pass
found no mutable parameter defaults or wildcard imports. Type annotations are
now lint-enforced; a full mypy or pyright analysis remains a separate task.
Tests retain exceptions for dynamic doubles, docstrings, and long fixture text.

Regression tests cover model verification before saving, rejection without a
restart, GitHub pull context, partial link-fetch failure, unexpected programming
errors, source search limits, and unreadable source logging. The existing suite
covers implementation, repair, queue cleanup, persistence, and shared-core
behavior. Passing tests does not establish exhaustive coverage of every function.
