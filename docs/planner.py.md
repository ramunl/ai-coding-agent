# planner.py

**Purpose**: Plan features and assess bug reports using Claude AI, with repository context enrichment.

## Overview
Uses Anthropic's Claude API to generate implementation plans for features and assess whether bug reports have enough information for a coding agent to proceed.

## Initialization

### Client Setup
```python
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
```

A global Anthropic client is initialized at module load time.

## Key Functions

### `kotlin_file_sample() -> str`
Gathers a sample of Kotlin files from the repository.

**Process**:
1. Searches for .kt files recursively in `REPO_PATH`
2. Filters out files in "build" directories
3. Collects up to 50 relative paths
4. Returns newline-separated list

**Purpose**: Provides Claude with project structure context

### `plan_feature(feature_description: str) -> str`
Generates a detailed implementation plan for a feature.

**Process**:
1. Gathers Kotlin file sample for context
2. Enriches feature description with link contexts
3. Sends to Claude with project context
4. Returns detailed plan

**Claude Context**:
- Role: Senior Android architect
- Project: Multi-module IPTV app (Channel Cast)
- Modules:
  - app/ - Main app module
  - data-* - Data layer (storage, network, repository, prefs)
  - ui-* - UI layer (features, core, models)
  - channel-health-monitor/
  - proxy-health-monitor/

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
- Role: Triaging bug reports for an Android coding agent
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
