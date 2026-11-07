# planner.py

**Purpose**: Plan features and assess bug reports using Claude AI, with repository context enrichment.

## Overview
Uses Anthropic's Claude API to generate implementation plans for features and assess whether bug reports have enough information for a coding agent to proceed.

## Module layout

`planner.py` builds the prompts and re-exports the helpers it uses, so
`from ai_agent.planner import ...` keeps working. The helpers live in:

- `planning_provider.py` — provider selection, Codex/Claude calls, schema paths
- `repository_context.py` — repository sampling, bounded codebase search, and the
  bounded live-context builder for the tool-free API planner
- `bugfix_questions.py` — triage parsing and clarification-question filtering

Patch a name in the module where it is looked up (for example
`ai_agent.planning_provider.run`, `ai_agent.repository_context.active_project`).
These modules never import `planner`.

## Initialization

### Client Setup
`planning_provider.client()` creates the Anthropic client on the first Claude
request and caches it; importing the modules creates no client.

## Key Functions

### `repo_file_sample() -> str`
Gathers a sample of source files from the active project's repository.

**Process**:
1. Searches recursively in the active project's `repo_path` for files matching `SOURCE_EXTENSIONS`
   (covers common Android/Kotlin/Java as well as Python, JS/TS, Go, Ruby, Rust, C/C++, etc.)
2. Filters out files under `EXCLUDED_DIR_NAMES` (build, node_modules, .git, venv, dist, target, ...)
3. Collects up to 50 relative paths
4. Returns newline-separated list

**Purpose**: Provides Claude with project structure context, regardless of which registered project is active

### `planner_context(query, repo_path, *, time_budget_s, char_budget) -> str`
Builds bounded live repository context for the tool-free Anthropic API planner.

**Process**:
1. Rejects a missing, unreadable, relative or symlink-reaching repository root with
   a clear `ContextBlockedError`
2. Walks source files (reusing `_source_paths`) plus manifest/build/test files
   (`package.json`, `pyproject.toml`, `Makefile`, READMEs, CI workflows, …),
   skipping `EXCLUDED_DIR_NAMES`
3. Reads each file through `repository_read`, which rejects path escapes, symlinks
   and nonregular files, and redacts secret-bearing lines with `_redact_text`
4. Emits one block per file under its relative path, truncating per-file chars and
   marking truncation
5. Bounds the whole block by file count, elapsed time and total chars, and labels
   the output as untrusted repository data

**Purpose**: the Claude API planner has no repository tools, so this block supplies
bounded evidence in the prompt. CLI planners (Claude Code, Codex) already have
read/search tools and never receive it.

### `plan_feature(feature_description: str) -> str`
Generates a detailed implementation plan for a feature.

**Process**:
1. Gathers a repo file sample for context
2. Appends the live repository context block when the planner is the Claude API
   (`CLAUDE_PLANNER_VIA=api`); CLI planners keep the filename sample only
3. Enriches feature description with link contexts
4. Sends to Claude with project context
5. Returns detailed plan

**Claude Context**:
- Role: Senior software engineer
- Project: The active project's `github_repository` (from `active_project()`)

**Claude Output**:
1. Branch name (feature/xxx format)
2. Files to create/modify
3. Step-by-step implementation plan
4. Ready-to-use Codex prompt

**Configuration**:
- Model: From `ANTHROPIC_MODEL` config
- Max tokens: 2000

### `build_bugfix_prompt(bug_description: str) -> str`
Builds a focused prompt for fixing a reported bug.

**Enriches** bug description with link contexts

**Prompt Structure**:
1. Bug report context
2. Requirements:
   - Inspect existing implementation first
   - Keep fix focused on reported bug
   - Add/update focused tests when practical
   - Run relevant tests/checks
   - Avoid unrelated refactors

**Returns**: Formatted prompt string ready for Codex

### `assess_bugfix_report(bug_description: str) -> str`
Assesses whether a bug report has enough information for a coding agent.

**Process**:
1. Enriches bug description with link contexts
2. Sends to Claude for triage assessment
3. Returns structured response

**Claude Instructions**:
- Role: Triaging bug reports for a coding agent
- Task: Decide if agent can start fixing without more info
- Constraints: Only ask for necessary information
- Max 3 questions

**Response Formats**:

**If ready**:
```
READY
```

**If more info needed**:
```
QUESTIONS:
1. <short necessary question>
2. <short necessary question>
```

**Configuration**:
- Model: From `ANTHROPIC_MODEL` config
- Max tokens: 800

### `bugfix_questions(assessment: str) -> str | None`
Extracts questions from a bugfix assessment.

**Returns**:
- `None` if assessment starts with "READY"
- Question text if assessment starts with "QUESTIONS:"
- Full assessment text otherwise

**Usage**:
```python
assessment = assess_bugfix_report(bug)
questions = bugfix_questions(assessment)
if questions:
    print(f"Need more info:\n{questions}")
else:
    print("Ready to fix!")
```

## Configuration Dependencies

- `ANTHROPIC_KEY`: Claude API key
- `ANTHROPIC_MODEL`: Claude model version
- `REPO_PATH`: Path to target repository
- `CLAUDE_PLANNER_VIA`: `"api"` enables the tool-free API planner that receives the
  live repository context block; `"code"` (default) uses Claude Code with tools
- `PLANNER_CONTEXT_MAX_CHARS`: total char budget for the live context block
- `PLANNER_CONTEXT_SCAN_SECONDS`: elapsed-time budget for collecting the block
- `PLANNER_CONTEXT_ESCALATION`: `"codex"` escalates to the Codex CLI (which has
  repository tools) when the repository has no usable evidence; the default `"off"`
  raises the explicit context error instead

## Link Enrichment

Both feature and bug descriptions are enriched using `enrich_feature_description` from `github_links.py`:
- Extracts all GitHub references and web links
- Fetches context from linked issues, PRs, commits, files, and web pages
- Includes full context in the description sent to Claude

## Usage Examples

### Plan a Feature
```python
from ai_agent.planner import plan_feature

feature = "Add offline mode support"
plan = plan_feature(feature)
print(plan)
```

### Assess a Bug Report
```python
from ai_agent.planner import assess_bugfix_report, bugfix_questions

bug = "App crashes when loading large playlists"
assessment = assess_bugfix_report(bug)
qs = bugfix_questions(assessment)
if qs:
    print(f"Questions needed: {qs}")
    # User provides answers
    bug_with_answers = bug + "\n\nUser clarification: ..."
    plan = build_bugfix_prompt(bug_with_answers)
else:
    print("Ready to proceed with fix")
```
