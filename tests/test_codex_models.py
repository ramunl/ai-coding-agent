"""Codex selection uses metadata and persists an override without inference."""

import asyncio
import importlib
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ai_agent import codex_models


def test_catalog_pages_filters_hidden_and_preserves_model_slug():
    pages = [
        {
            "data": [
                {"id": "display-id", "model": "codex-test"},
                {"model": "hidden", "hidden": True},
            ],
            "nextCursor": "next",
        },
        {"data": [{"model": "second"}], "nextCursor": None},
    ]
    with patch.object(
        codex_models, "read_codex_response", AsyncMock(side_effect=pages)
    ) as read:
        ok, models = codex_models.list_models()
    assert ok
    assert [entry["id"] for entry in models] == ["default", "codex-test", "second"]
    assert read.call_args.args == (
        "model/list",
        {"limit": 100, "includeHidden": False, "cursor": "next"},
    )


def test_failed_catalog_is_sanitized_and_unknown_models_are_refused():
    with patch.object(
        codex_models,
        "read_codex_response",
        AsyncMock(side_effect=RuntimeError("secret")),
    ):
        ok, detail = codex_models.verify_model("missing")
    assert not ok
    assert "secret" not in detail
    with patch.object(
        codex_models, "list_models", return_value=(True, [{"id": "known"}])
    ):
        assert codex_models.verify_model("known")[0]
        assert not codex_models.verify_model("missing")[0]
    with patch.object(codex_models, "list_models") as read:
        assert codex_models.verify_model("default")[0]
        assert not codex_models.verify_model("bad\nCODEX_MODEL=x")[0]
        read.assert_not_called()


def test_save_updates_environment_and_persistent_file_without_restart():
    with patch.dict(os.environ, TELEGRAM_BOT_TOKEN="t", YOUR_CHAT_ID="1"):
        actions = importlib.import_module("ai_agent.actions")
        tool = actions.get_tool("codex")
        manager = importlib.import_module("ai_agent.ai_tools").model_manager
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "agent.env"
            env_file.write_text("OTHER=keep\nCODEX_MODEL=old\n")
            with (
                patch.object(manager, "ENV_FILE", env_file),
                patch.object(tool, "verify", return_value=(True, "listed")),
                patch.object(actions, "schedule_restart") as restart,
            ):
                result = asyncio.run(actions.switch_model("codex", "codex-test"))
                assert "next run" in result
                assert codex_models.model_args() == ["--model", "codex-test"]
                assert env_file.read_text() == "OTHER=keep\nCODEX_MODEL=codex-test\n"
                restart.assert_not_called()
                tool.set_model("default")
                assert codex_models.model_args() == []
                assert "CODEX_MODEL=default" in env_file.read_text()
