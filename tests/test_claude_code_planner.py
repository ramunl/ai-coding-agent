"""Claude Code planning: read-only, on the subscription, JSON out, usage counted."""

import importlib
import json
from types import SimpleNamespace

import pytest

PLAN = {"summary": "Add login", "branch": "feature/login", "steps": ["a"]}


def stream(*events) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


LIMITS = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "unifiedWindows": {"five_hour": {"utilization": 0.25, "resetsAt": 1800000000}}
    },
}


def result(text, **extra):
    return {"type": "result", "result": text, "is_error": False, **extra}


@pytest.fixture
def planner(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("YOUR_CHAT_ID", "123")
    module = importlib.import_module("ai_agent.claude_code_planner")
    monkeypatch.setattr(module, "attachments", lambda: ([], 0))
    usage = []
    monkeypatch.setattr(module, "record_usage", usage.append)
    schema = tmp_path / "plan.json"
    schema.write_text(
        '{"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}'
    )
    return SimpleNamespace(
        module=module, usage=usage, schema=schema, limits=tmp_path / "limits.json"
    )


def fake_runner(output, calls):
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(output=output)

    return runner


def test_plans_read_only_on_stdin_and_returns_the_json(planner):
    calls = []
    reply = "Here is the plan:\n```json\n" + json.dumps(PLAN) + "\n```"
    output = stream(
        {"type": "system", "subtype": "init"},
        LIMITS,
        result(reply, usage={"input_tokens": 1200, "output_tokens": 300}),
    )
    text = planner.module.claude_code_message(
        "Plan: add login", planner.schema, fake_runner(output, calls), planner.limits
    )
    assert json.loads(text) == PLAN
    (args, kwargs) = calls[0]
    assert args[:2] == ["claude", "-p"]
    assert args[args.index("--allowedTools") + 1] == "Read,Grep,Glob"
    denied = args[args.index("--disallowedTools") + 1].split(",")
    assert {"Bash", "Edit", "Write"} <= set(denied)
    assert "--dangerously-skip-permissions" not in args
    assert "acceptEdits" not in args
    sent = json.loads(kwargs["input_text"])
    assert sent["message"]["content"].startswith("Plan: add login")
    assert args[args.index("--json-schema") + 1] == '{"type":"object"}'
    assert kwargs["private"] is False
    assert planner.usage[0].tool == "claude"
    assert planner.usage[0].tokens == 1500
    (window,) = json.loads(planner.limits.read_text())["windows"]
    assert (window["window_minutes"], window["remaining_percent"]) == (300, 75.0)


def test_structured_output_is_the_plan(planner):
    output = stream(result("ignored prose", structured_output=PLAN))
    text = planner.module.claude_code_message(
        "Plan", planner.schema, fake_runner(output, []), planner.limits
    )
    assert json.loads(text) == PLAN


def test_images_go_in_the_stdin_message_privately(planner, monkeypatch):
    monkeypatch.setattr(
        planner.module, "attachments", lambda: ([("image/png", b"1234")], 0)
    )
    calls = []
    output = stream(result(json.dumps(PLAN)))
    planner.module.claude_code_message(
        "Plan", planner.schema, fake_runner(output, calls), planner.limits
    )
    (args, kwargs) = calls[0]
    content = json.loads(kwargs["input_text"])["message"]["content"]
    assert content[0]["type"] == "image"
    assert kwargs["private"] is True
    assert all("1234" not in part for part in args)


@pytest.mark.parametrize(
    "error_text",
    [
        "Command failed (1): claude -p …\n"
        + stream(
            result("You've hit your limit · resets 3pm (Europe/Moscow)", is_error=True)
        ),
        "Command failed (1): claude -p …\nYou've hit your limit · resets 3pm\n",
    ],
)
def test_a_usage_limit_is_shown_in_words(planner, error_text):
    def runner(args, **kwargs):
        raise RuntimeError(error_text)

    with pytest.raises(RuntimeError) as caught:
        planner.module.claude_code_message(
            "Plan", planner.schema, runner, planner.limits
        )
    assert str(caught.value).startswith("Claude Code planning failed: You've hit")
    assert "{" not in str(caught.value)


def test_an_error_result_is_a_failure_not_a_plan(planner):
    output = stream(result("Not logged in · Please run /login", is_error=True))
    with pytest.raises(RuntimeError, match="Not logged in"):
        planner.module.claude_code_message(
            "Plan", planner.schema, fake_runner(output, []), planner.limits
        )
    assert planner.usage == []


def test_planning_provider_uses_claude_code_unless_told_api(planner, monkeypatch):
    provider = importlib.import_module("ai_agent.planning_provider")
    monkeypatch.setattr(provider, "diagnostic_context", lambda: "")
    seen = []
    monkeypatch.setattr(
        provider,
        "claude_code_message",
        lambda prompt, schema: seen.append(schema.name) or "{}",
    )
    monkeypatch.setattr(provider, "CLAUDE_PLANNER_VIA", "code")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert provider.planner_message("x", "claude", "feature_plan.json", 100) == "{}"
    assert seen == ["feature_plan.json"]
    monkeypatch.setattr(provider, "CLAUDE_PLANNER_VIA", "api")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not configured"):
        provider.planner_message("x", "claude", "feature_plan.json", 100)
