"""Bugfix readiness must not hide oversized implementation scope."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ai_agent.bugfix_scope import parse_bugfix_review


def test_legacy_ready_and_questions_remain_compatible():
    assert (
        parse_bugfix_review('{"status":"ready","questions":[]}', "Fix label").questions
        is None
    )
    result = parse_bugfix_review(
        '{"status":"questions","questions":["What should happen?"]}', "bug"
    )
    assert result.questions == "What should happen?"
    assert not result.scope_assessment["recommend_split"]


def test_broad_ready_bugfix_warns_before_confirm_and_preserves_fix_semantics():
    from ai_agent.bot import planning

    review = parse_bugfix_review(
        json.dumps(
            {
                "status": "ready",
                "questions": [],
                "files": ["worker.py", "storage.py"],
                "steps": [
                    "Persist checkpoint",
                    "Resume worker",
                    "Handle cancellation",
                    "Test restart",
                ],
            }
        ),
        "Interrupted work is lost",
    )
    assert review.scope_assessment["recommend_split"]
    context = SimpleNamespace(user_data={})
    reply = AsyncMock()
    identity = {
        "project_name": "repo",
        "repo_path": "/tmp/repo",
        "github_repository": "owner/repo",
    }
    with (
        patch.object(
            planning.asyncio, "to_thread", AsyncMock(return_value="Fix actual bug")
        ),
        patch.object(planning, "build_bugfix_prompt", return_value="Fix actual bug"),
        patch.object(planning, "reply_chunks", reply),
    ):
        asyncio.run(
            planning.prepare_bugfix(
                None, context, "Lost work", "Lost work", identity, [], review
            )
        )
    pending = context.user_data["pending_implementation"]
    assert pending["commit_type"] == "fix"
    assert pending["codex_prompt"] == "Fix actual bug"
    assert pending["scope_assessment"] == review.scope_assessment
    assert "pending_plan" not in context.user_data
    text = reply.await_args.args[1]
    assert "Scope warning" in text
    assert "Persist checkpoint" in text
    assert "/cancel then /plan" in text
    assert text.index("Scope warning") < text.index("Send /confirm")


def test_bugfix_scope_fields_optional_in_schema():
    from ai_agent.planning_provider import SCHEMAS_DIR

    schema = json.loads((SCHEMAS_DIR / "bugfix_assessment.json").read_text())
    assert schema["required"] == ["status", "questions"]
    assert "scope_assessment" in schema["properties"]
