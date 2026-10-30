import importlib
import inspect
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
# Modules bound to the fake anthropic package; reloaded around every test.
PLANNING_MODULES = ("ai_agent.planner", "ai_agent.planning_provider")
NEW_MODULES = (
    "ai_agent.planning_provider",
    "ai_agent.repository_context",
    "ai_agent.bugfix_questions",
    "ai_agent.plan_document",
)


class PlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_env = {
            "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
            "YOUR_CHAT_ID": os.environ.get("YOUR_CHAT_ID"),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
        }
        os.environ["TELEGRAM_BOT_TOKEN"] = "telegram-secret"
        os.environ["YOUR_CHAT_ID"] = "123"
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-secret"
        # These tests cover the API path; Claude Code planning has its own tests.
        via = patch("ai_agent.config.CLAUDE_PLANNER_VIA", "api")
        via.start()
        self.addCleanup(via.stop)

        self.previous_anthropic = sys.modules.get("anthropic")
        sys.modules.pop("anthropic", None)
        for name in PLANNING_MODULES:
            sys.modules.pop(name, None)

        anthropic_module = types.ModuleType("anthropic")

        class FakeAnthropic:
            def __init__(self, api_key: str, **kwargs) -> None:
                self.messages = FakeMessages()

        class FakeMessages:
            def create(self, **kwargs):
                self.kwargs = kwargs
                return types.SimpleNamespace(
                    content=[types.SimpleNamespace(type="text", text="planned")]
                )

        self.fake_anthropic_class = FakeAnthropic
        anthropic_module.Anthropic = FakeAnthropic
        anthropic_module.DefaultHttpxClient = lambda **kwargs: object()
        anthropic_module.NotFoundError = type("NotFoundError", (Exception,), {})
        self.anthropic_module = anthropic_module
        sys.modules["anthropic"] = anthropic_module

    def tearDown(self) -> None:
        for key, value in self.previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        for name in PLANNING_MODULES:
            sys.modules.pop(name, None)
        sys.modules.pop("anthropic", None)
        if self.previous_anthropic is not None:
            sys.modules["anthropic"] = self.previous_anthropic

    def _modules(self):
        planner = importlib.import_module("ai_agent.planner")
        provider = importlib.import_module("ai_agent.planning_provider")
        return planner, provider

    def _codex_args(self, provider, schema_name: str, run_side_effect=None):
        with (
            patch("ai_agent.planning_provider.run") as run_mock,
            patch.dict(os.environ, CODEX_MODEL="codex-test"),
        ):

            def write_output(args, timeout):
                from ai_agent.shell import CommandResult

                output_path = args[args.index("--output-last-message") + 1]
                with open(output_path, "w", encoding="utf-8") as output:
                    output.write('{"status":"ready","questions":[]}')
                return CommandResult(args, 0, "")

            run_mock.side_effect = run_side_effect or write_output
            result = provider._codex_message("triage this", schema_name)
        return result, run_mock

    def test_plan_feature_uses_enriched_feature_description(self) -> None:
        planner, provider = self._modules()

        with patch("ai_agent.planner.repo_file_sample", return_value="App.kt"):
            with patch(
                "ai_agent.planner.enrich_feature_description",
                return_value="enriched request",
            ):
                result = planner.plan_feature("original request", "claude")

        prompt = provider.client().messages.kwargs["messages"][0]["content"]
        self.assertEqual(result, "planned")
        self.assertIn("Feature request: enriched request", prompt)
        self.assertIn("Project files sample:\nApp.kt", prompt)
        self.assertIn("already split or repository-scoped task", prompt)
        self.assertIn('assigned repository in "repositories"', prompt)

    def test_plan_feature_injects_rules_block(self) -> None:
        planner, provider = self._modules()

        with patch("ai_agent.planner.repo_file_sample", return_value="App.kt"):
            with patch(
                "ai_agent.planner.enrich_feature_description",
                return_value="enriched request",
            ):
                with patch(
                    "ai_agent.planner.rules_prompt_block",
                    return_value="\nMANDATORY CODING RULES\n- Avoid return operators\n",
                ):
                    planner.plan_feature("original request", "claude")

        prompt = provider.client().messages.kwargs["messages"][0]["content"]
        self.assertIn("MANDATORY CODING RULES", prompt)
        self.assertIn("Avoid return operators", prompt)

    def test_revise_feature_plan_sends_current_plan_and_feedback(self) -> None:
        planner, provider = self._modules()

        with patch(
            "ai_agent.planner.enrich_feature_description", return_value="enriched"
        ):
            result = planner.revise_feature_plan(
                "original", '{"summary": "v1"}', "split it up", "claude"
            )

        kwargs = provider.client().messages.kwargs
        prompt = kwargs["messages"][0]["content"]
        self.assertEqual(result, "planned")
        self.assertEqual(kwargs["max_tokens"], 4000)
        self.assertIn('Current plan:\n{"summary": "v1"}', prompt)
        self.assertIn("User feedback:\nsplit it up", prompt)
        self.assertIn("Original feature request:\nenriched", prompt)

    def test_plan_revision_and_bugfix_prompts_carry_the_project_brief(self) -> None:
        planner = importlib.import_module("ai_agent.planner")
        brief = "\nPROJECT BRIEF (the repository's AGENTS.md)\nA cast app.\n"
        with (
            patch("ai_agent.planner.project_brief_block", return_value=brief),
            patch("ai_agent.planner.ecosystem_block", return_value="\nREPOS\n"),
            patch("ai_agent.planner.rules_prompt_block", return_value="\nRULES\n"),
            patch("ai_agent.planner.repo_file_sample", return_value="App.kt"),
            patch("ai_agent.planner.enrich_feature_description", side_effect=str),
        ):
            planner.plan_feature("add queue", "claude")
            prompts = [
                importlib.import_module("ai_agent.planning_provider")
                .client()
                .messages.kwargs["messages"][0]["content"]
            ]
            planner.revise_feature_plan("add queue", "{}", "smaller", "claude")
            prompts.append(
                importlib.import_module("ai_agent.planning_provider")
                .client()
                .messages.kwargs["messages"][0]["content"]
            )
            prompts.append(planner.build_bugfix_prompt("crash on start"))
        for prompt in prompts:
            self.assertIn("A cast app.", prompt)
            self.assertLess(prompt.index("A cast app."), prompt.index("RULES"))
            self.assertLess(prompt.index("REPOS"), prompt.index("RULES"))
        for prompt in prompts[:2]:
            self.assertIn('"repositories": [{"name"', prompt)

    def test_build_bugfix_prompt_uses_enriched_bug_description(self) -> None:
        planner, _ = self._modules()

        with (
            patch(
                "ai_agent.planner.enrich_feature_description",
                return_value="enriched bug",
            ),
            patch(
                "ai_agent.planner.codebase_search_context",
                return_value="SEARCH HINTS",
            ),
        ):
            prompt = planner.build_bugfix_prompt("original bug")

        self.assertIn("Fix this bug", prompt)
        self.assertIn("Bug report:\nenriched bug", prompt)
        self.assertIn("pre-search:\nSEARCH HINTS", prompt)
        self.assertIn("Do not make unrelated refactors", prompt)

    def test_assess_bugfix_report_uses_enriched_bug_description(self) -> None:
        planner, provider = self._modules()

        with patch(
            "ai_agent.planner.enrich_feature_description", return_value="enriched bug"
        ):
            result = planner.assess_bugfix_report("original bug", "claude")

        kwargs = provider.client().messages.kwargs
        prompt = kwargs["messages"][0]["content"]
        self.assertEqual(result, "planned")
        self.assertEqual(kwargs["max_tokens"], 500)
        self.assertIn("Bug report:\nenriched bug", prompt)

    def test_codex_planner_uses_read_only_structured_output(self) -> None:
        _, provider = self._modules()

        result, run_mock = self._codex_args(provider, "bugfix_assessment.json")

        args = run_mock.call_args.args[0]
        self.assertEqual(result, '{"status":"ready","questions":[]}')
        self.assertEqual(args[args.index("--model") + 1], "codex-test")
        self.assertIn("--sandbox", args)
        self.assertIn("read-only", args)
        self.assertIn("--ephemeral", args)
        self.assertIn("--output-schema", args)

    def test_codex_planner_resolves_bundled_schema_file(self) -> None:
        planner, provider = self._modules()

        _, run_mock = self._codex_args(provider, "feature_plan.json")

        args = run_mock.call_args.args[0]
        schema = Path(args[args.index("--output-schema") + 1])
        self.assertEqual(
            schema, REPO_ROOT / "ai_agent" / "schemas" / "feature_plan.json"
        )
        self.assertTrue(schema.is_file())
        self.assertTrue((provider.SCHEMAS_DIR / "bugfix_assessment.json").is_file())
        self.assertIs(planner.SCHEMAS_DIR, provider.SCHEMAS_DIR)

    def test_codex_planner_passes_configured_timeout_and_propagates_it(self) -> None:
        _, provider = self._modules()
        config = importlib.import_module("ai_agent.config")

        _, run_mock = self._codex_args(provider, "feature_plan.json")
        self.assertEqual(
            run_mock.call_args.kwargs["timeout"], config.CODEX_TIMEOUT_SECONDS
        )

        timed_out = RuntimeError("Command timed out after 1800 seconds")
        with self.assertRaisesRegex(RuntimeError, "timed out after 1800"):
            self._codex_args(provider, "feature_plan.json", timed_out)

    def test_codex_capacity_failure_becomes_a_short_error(self) -> None:
        _, provider = self._modules()
        errors = importlib.import_module("ai_agent.model_errors")
        failure = RuntimeError(f"prompt echo ... {errors.CODEX_CAPACITY_MARKER}")

        with self.assertRaises(errors.CodexCapacityError):
            self._codex_args(provider, "feature_plan.json", failure)

    def test_claude_rejected_model_becomes_a_clear_error(self) -> None:
        _, provider = self._modules()

        with patch.object(
            provider.client().messages,
            "create",
            side_effect=self.anthropic_module.NotFoundError("404"),
        ):
            with self.assertRaisesRegex(RuntimeError, "is unavailable"):
                provider.planner_message("prompt", "claude", "plan.json", 4000)

    def test_claude_planner_requires_an_api_key(self) -> None:
        _, provider = self._modules()

        with (
            patch.dict(os.environ, ANTHROPIC_API_KEY=""),
            patch.object(provider, "_create_message") as create,
        ):
            with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY is not"):
                provider.planner_message("prompt", "claude", "plan.json", 4000)
        create.assert_not_called()

    def test_unsupported_planning_agent_is_rejected(self) -> None:
        planner, _ = self._modules()

        with self.assertRaisesRegex(ValueError, "Unsupported planning agent: gpt"):
            planner.normalize_planning_agent("gpt")
        self.assertEqual(planner.normalize_planning_agent(" Claude "), "claude")
        self.assertEqual(planner.planning_agent_label("codex"), "Codex")

    def test_bugfix_questions_parses_ready_and_questions(self) -> None:
        planner, _ = self._modules()

        self.assertIsNone(planner.bugfix_questions("READY"))
        self.assertEqual(planner.bugfix_questions("QUESTIONS:\n1. Steps?"), "1. Steps?")

    def test_bugfix_questions_drops_implementation_questions(self) -> None:
        planner, _ = self._modules()
        assessment = (
            '{"status":"questions","questions":'
            '["1. Where is the button?", "2. Should it sort by date?"]}'
        )

        self.assertEqual(
            planner.bugfix_questions(assessment), "2. Should it sort by date?"
        )
        self.assertIsNone(planner.bugfix_questions('{"status":"ready"}'))
        self.assertIsNone(planner.bugfix_questions("QUESTIONS:\nWhat method?"))
        self.assertEqual(
            planner.bugfix_questions('{"status":"questions","questions":[]}'),
            "Please provide the missing expected product behavior.",
        )

    def test_search_context_bounds_results_and_excludes_generated_files(self) -> None:
        context_module = importlib.import_module("ai_agent.repository_context")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "player.py").write_text(
                "playback = 1\nplayback = 2\nplayback = 3\n"
            )
            (root / "node_modules").mkdir()
            (root / "node_modules" / "hidden.py").write_text("playback = 99\n")
            with patch.object(
                context_module,
                "active_project",
                return_value=types.SimpleNamespace(repo_path=root),
            ):
                context = context_module.codebase_search_context(
                    "playback", max_files=1, max_matches=2
                )

        self.assertIn("player.py:1: playback = 1", context)
        self.assertIn("player.py:2: playback = 2", context)
        self.assertNotIn("playback = 3", context)
        self.assertNotIn("hidden.py", context)

    def test_search_context_truncates_long_lines_and_uses_default_bounds(self) -> None:
        context_module = importlib.import_module("ai_agent.repository_context")
        long_value = "x" * 400
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "player.py").write_text(f"playback = '{long_value}'\n")
            with patch.object(
                context_module,
                "active_project",
                return_value=types.SimpleNamespace(repo_path=root),
            ):
                context = context_module.codebase_search_context("playback")

        match = context.split("Relevant matches:\n", 1)[1]
        self.assertEqual(
            len(match), len("player.py:1: ") + context_module.MATCH_LINE_LENGTH
        )
        self.assertEqual(context_module.MATCH_LINE_LENGTH, 220)
        defaults = inspect.signature(context_module.codebase_search_context).parameters
        self.assertEqual(defaults["max_files"].default, 80)
        self.assertEqual(defaults["max_matches"].default, 80)
        self.assertEqual(defaults["max_files"].default, context_module.SEARCH_MAX_FILES)

    def test_search_context_reports_missing_repository(self) -> None:
        context_module = importlib.import_module("ai_agent.repository_context")
        missing = Path("/nonexistent/ai-agent-repo")
        with patch.object(
            context_module,
            "active_project",
            return_value=types.SimpleNamespace(repo_path=missing),
        ):
            context = context_module.codebase_search_context("playback")

        self.assertEqual(context, f"Repository path does not exist: {missing}")

    def test_repo_file_sample_is_bounded(self) -> None:
        context_module = importlib.import_module("ai_agent.repository_context")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(context_module.SAMPLE_FILE_LIMIT + 10):
                (root / f"module{index}.py").write_text("")
            (root / "notes.txt").write_text("")
            with patch.object(
                context_module,
                "active_project",
                return_value=types.SimpleNamespace(repo_path=root),
            ):
                sample = context_module.repo_file_sample().splitlines()

        self.assertEqual(len(sample), 50)
        self.assertEqual(len(sample), context_module.SAMPLE_FILE_LIMIT)
        self.assertNotIn("notes.txt", sample)

    def test_search_context_logs_unreadable_source_and_keeps_file_list(self) -> None:
        context_module = importlib.import_module("ai_agent.repository_context")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "player.py").write_text("playback = 1\n")
            with (
                patch.object(
                    context_module,
                    "active_project",
                    return_value=types.SimpleNamespace(repo_path=root),
                ),
                patch.object(
                    context_module, "repository_read", side_effect=OSError("unreadable")
                ),
                self.assertLogs("ai_agent.repository_context", level="WARNING") as logs,
            ):
                context = context_module.codebase_search_context("playback")

        self.assertIn("Files:\nplayer.py", context)
        self.assertIn("unreadable", logs.output[0])

    def test_planner_reads_text_after_thinking_blocks(self):
        _, provider = self._modules()
        response = types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="thinking", thinking="private reasoning"),
                types.SimpleNamespace(type="text", text="first"),
                types.SimpleNamespace(type="text", text="second"),
            ]
        )
        with patch.object(provider, "_create_message", return_value=response):
            self.assertEqual(
                provider.planner_message("prompt", "claude", "plan.json", 4000),
                "first\nsecond",
            )

    def test_planner_reports_missing_text_without_exposing_thinking(self):
        _, provider = self._modules()
        response = types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="thinking", thinking="private reasoning")
            ]
        )
        with patch.object(provider, "_create_message", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Claude returned no text"):
                provider.planner_message("prompt", "claude", "plan.json", 4000)

    def test_claude_planner_counts_input_plus_output_tokens(self):
        _, provider = self._modules()
        response = types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="plan")],
            usage=types.SimpleNamespace(input_tokens=30, output_tokens=12),
        )
        with (
            patch.object(provider, "_create_message", return_value=response),
            patch.object(provider, "record_usage") as record,
        ):
            provider.planner_message("prompt", "claude", "plan.json", 4000)
        recorded = record.call_args.args[0]
        self.assertEqual(
            (recorded.role, recorded.tool, recorded.tokens), ("planner", "claude", 42)
        )

    def test_claude_planner_without_usage_records_unknown(self):
        _, provider = self._modules()
        response = types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="plan")]
        )
        with (
            patch.object(provider, "_create_message", return_value=response),
            patch.object(provider, "record_usage") as record,
        ):
            provider.planner_message("prompt", "claude", "plan.json", 4000)
        self.assertIsNone(record.call_args.args[0].tokens)

    def test_codex_planner_counts_the_total_it_reports(self):
        from ai_agent.shell import CommandResult

        _, provider = self._modules()

        def run_side_effect(args, timeout):
            output_path = args[args.index("--output-last-message") + 1]
            Path(output_path).write_text('{"status":"ready","questions":[]}')
            return CommandResult(args, 0, "tokens used\n77")

        with patch.object(provider, "record_usage") as record:
            self._codex_args(provider, "bugfix_assessment.json", run_side_effect)
        recorded = record.call_args.args[0]
        self.assertEqual(
            (recorded.role, recorded.tool, recorded.tokens), ("planner", "codex", 77)
        )

    def test_old_planner_import_paths_resolve_to_moved_objects(self) -> None:
        planner, provider = self._modules()
        context_module = importlib.import_module("ai_agent.repository_context")
        questions = importlib.import_module("ai_agent.bugfix_questions")
        moved = {
            provider: (
                "SCHEMAS_DIR",
                "SUPPORTED_PLANNING_AGENTS",
                "normalize_planning_agent",
                "planning_agent_label",
            ),
            context_module: (
                "EXCLUDED_DIR_NAMES",
                "SOURCE_EXTENSIONS",
                "codebase_search_context",
                "repo_file_sample",
            ),
            questions: ("IMPLEMENTATION_QUESTION_PATTERNS", "bugfix_questions"),
        }
        for module, names in moved.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertIn(name, planner.__all__)
                    self.assertIs(getattr(planner, name), getattr(module, name))


class ModuleBoundaryTests(unittest.TestCase):
    def test_new_modules_import_cleanly_without_planner_or_plan_state(self) -> None:
        env = {
            **os.environ,
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "YOUR_CHAT_ID": "123",
        }
        for module in NEW_MODULES:
            with self.subTest(module=module):
                code = (
                    "import importlib, sys\n"
                    f"importlib.import_module({module!r})\n"
                    "leaked = {'ai_agent.planner', 'ai_agent.plan_state'}"
                    " & set(sys.modules)\n"
                    "assert not leaked, leaked\n"
                )
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_importing_planning_provider_creates_no_client(self) -> None:
        code = (
            "import anthropic\n"
            "def refuse(*args, **kwargs):\n"
            "    raise AssertionError('client created at import time')\n"
            "anthropic.Anthropic = refuse\n"
            "import ai_agent.planning_provider as provider\n"
            "assert provider.client.cache_info().currsize == 0\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            env={**os.environ, "ANTHROPIC_API_KEY": "anthropic-secret"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
