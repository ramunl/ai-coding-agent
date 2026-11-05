#!/usr/bin/env bash
# Reproducible baseline check for ai-coding-agent: one command that mirrors
# .github/workflows/test.yml locally. Offline and side-effect free — unit
# tests only (no network, no real /etc or /var/lib, no deploy/restart/backup
# mutation).
set -euo pipefail

# CI runs in a clean container, so drop the ambient production settings and
# live credentials that would otherwise skew the offline unit tests (e.g. the
# provider/auth defaults) or leak into them. PATH and everything else stay
# untouched, so pytest and ruff resolve exactly as they do in CI after
# `pip install -r requirements-dev.txt`.
unset \
    PLANNING_AGENT IMPLEMENTATION_AGENT CLAUDE_CODE_AUTH \
    CLAUDE_PLANNER_VIA CLAUDE_PLANNER_MODEL \
    ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN DEEPSEEK_API_KEY \
    GITHUB_TOKEN TELEGRAM_BOT_TOKEN CLAUDE_CODE_MESSAGING_TOKEN \
    YOUR_CHAT_ID

pytest

ruff check agent.py ai_agent tests deploy/claude-code-statusline.py
ruff format --check agent.py ai_agent tests deploy/claude-code-statusline.py
