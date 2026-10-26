import json
import types
import unittest
from unittest.mock import patch

import ai_agent.plan_document as plan_document
import ai_agent.plan_state as plan_state
from ai_agent.plan_state import (
    ExecutionState,
    PlanState,
    Verbosity,
    new_plan_state,
    parse_plan_document,
    plan_outline,
    render_completion,
    render_diff_summary,
    render_history,
    render_plan,
    revise_plan_state,
)

PROJECT = types.SimpleNamespace(name="repo", github_repository="owner/repo")


def _execution(**overrides) -> ExecutionState:
    values = {
        "branch": "feature/example",
        "files_changed": ["A.kt"],
        "diff_summary": "Modified files:\n1. A.kt",
        "full_diff": "diff --git a/A.kt b/A.kt",
        "logs": "verbose output",
        "pr_url": "https://github.example/pr/1",
        "tests": "PASS",
    }
    return ExecutionState(**{**values, **overrides})


class PlanStateTests(unittest.TestCase):
    def test_render_uses_saved_repository_even_after_switch(self) -> None:
        plan = new_plan_state(
            "feature",
            "text",
            {
                "project_name": "A",
                "repo_path": "/tmp/A",
                "github_repository": "owner/A",
            },
        )
        with patch.object(plan_state, "active_project", side_effect=AssertionError):
            self.assertIn("A (owner/A)", render_plan(plan))
            self.assertIn(
                "legacy plan; recreate", render_plan(new_plan_state("old", "text"))
            )

    def test_identity_is_explicit_and_preserved_by_revision(self) -> None:
        identity = {
            "project_name": "A",
            "repo_path": "/tmp/A",
            "github_repository": "owner/A",
        }
        plan = new_plan_state("feature", "first", identity)
        revised = revise_plan_state(plan, "second")
        self.assertEqual(revised.repository_identity, identity)
        self.assertIsNot(revised.repository_identity, plan.repository_identity)
        identity["project_name"] = "B"
        self.assertEqual(plan.repository_identity["project_name"], "A")
        self.assertIsNone(new_plan_state("legacy", "text").repository_identity)

    def test_parse_plan_document_reads_json_and_normalizes_branch(self) -> None:
        plan_text = json.dumps(
            {
                "branch": "feature/on/off:bad",
                "summary": "Add queue support",
                "files": ["CastSessionManager.kt"],
                "steps": ["Integrate Cast queue"],
                "risks": ["Reconnect behavior"],
                "codex_prompt": "Implement the plan",
            }
        )

        document = parse_plan_document(plan_text, "Add queue support")

        self.assertEqual(document.branch, "feature/on-off-bad")
        self.assertEqual(document.summary, "Add queue support")
        self.assertEqual(document.files, ["CastSessionManager.kt"])
        self.assertEqual(document.codex_prompt, "Implement the plan")

    def test_plan_without_codex_prompt_builds_an_instruction(self) -> None:
        # This is the shape Claude actually returns: no codex_prompt field.
        # The regression that shipped raw JSON to Codex lived exactly here.
        plan_text = (
            "```json\n"
            + json.dumps(
                {
                    "branch": "feature/telegram-rich-text",
                    "summary": "Add rich text to command responses",
                    "files": ["Formatter.kt"],
                    "steps": ["1. Add formatter", "2. Wire it in"],
                    "risks": ["Escaping is strict"],
                }
            )
            + "\n```"
        )

        document = parse_plan_document(plan_text, "improve readability")

        # The prompt must be a direct instruction, never the raw plan JSON.
        self.assertNotIn('"steps"', document.codex_prompt)
        self.assertNotIn("```", document.codex_prompt)
        self.assertIn("Edit the files directly", document.codex_prompt)
        self.assertIn("Add rich text to command responses", document.codex_prompt)
        self.assertIn("Formatter.kt", document.codex_prompt)
        # And the branch comes from the plan, not the slugified feature sentence.
        self.assertEqual(document.branch, "feature/telegram-rich-text")

    def test_truncated_plan_json_is_recovered_not_dumped_as_steps(self) -> None:
        # The real regression: the plan JSON is fenced but cut off before the
        # closing brace (the model hit its token budget). The old parser gave
        # up, rendering the fence line as the summary and the raw JSON lines as
        # numbered "steps". The repair must recover the completed fields.
        truncated = (
            "```json\n"
            "{\n"
            '  "branch": "feature/improve-command-response-text-readability",\n'
            '  "summary": "Improve readability of command response text",\n'
            '  "files": [\n'
            '    "ui-core/CommandResponseFormatter.kt",\n'
            '    "ui-core/CommandResponseFormatterTest.kt",\n'
            '    "ui-models/CommandResponse.kt",'
        )

        document = parse_plan_document(truncated, "improve readability")

        self.assertEqual(
            document.summary, "Improve readability of command response text"
        )
        self.assertEqual(document.branch, "feature/improve-command")
        # All three completed file entries survive; nothing is dumped as steps.
        self.assertEqual(len(document.files), 3)
        self.assertIn("ui-models/CommandResponse.kt", document.files)
        self.assertNotIn("```json", document.summary)

    def test_plan_json_with_prose_preamble_is_parsed(self) -> None:
        plan_text = (
            "Here is my implementation plan:\n\n```json\n"
            + json.dumps(
                {"branch": "feature/x", "summary": "Do the thing", "files": ["A.kt"]}
            )
            + "\n```\nLet me know if you'd like changes."
        )

        document = parse_plan_document(plan_text, "the thing")

        self.assertEqual(document.summary, "Do the thing")
        self.assertEqual(document.files, ["A.kt"])
        self.assertNotIn("Here is my", document.summary)

    def test_unparseable_plan_still_directs_implementation(self) -> None:
        document = parse_plan_document("just do the thing", "the thing")
        self.assertIn("Edit the files directly", document.codex_prompt)
        self.assertIn("just do the thing", document.codex_prompt)

    def test_render_plan_and_history_include_revision_data(self) -> None:
        plan = new_plan_state(
            "Feature", json.dumps({"summary": "First", "steps": ["Do it"]})
        )
        revised = revise_plan_state(
            plan, json.dumps({"summary": "Second", "steps": ["Do it differently"]})
        )

        self.assertIn("Revision: 2", render_plan(revised))
        self.assertIn("Revision 1: First", render_history(revised))
        self.assertIn("Revision 2: Second", render_history(revised))

    def test_render_completion_hides_details_in_concise_mode(self) -> None:
        execution = ExecutionState(
            branch="feature/example",
            files_changed=["A.kt"],
            diff_summary="Modified files:\n1. A.kt",
            full_diff="diff --git a/A.kt b/A.kt",
            logs="verbose output",
            pr_url="https://github.example/pr/1",
            tests="PASS",
        )

        output = render_completion(execution, Verbosity.CONCISE)

        self.assertIn("Files changed: 1", output)
        self.assertIn("Tests: PASS", output)
        self.assertNotIn("verbose output", output)
        self.assertNotIn("diff --git", output)

    def test_render_completion_reports_failed_ci_as_failure(self) -> None:
        execution = ExecutionState(
            branch="feature/example",
            files_changed=["A.kt"],
            diff_summary="Modified files:\n1. A.kt",
            full_diff="diff --git a/A.kt b/A.kt",
            logs="verbose output",
            pr_url="https://github.example/pr/1",
            tests="FAIL",
        )

        output = render_completion(execution, Verbosity.CONCISE)

        self.assertIn("Implementation failed.", output)
        self.assertNotIn("Implementation completed.", output)

    def test_render_completion_debug_lists_files_diff_and_logs(self) -> None:
        output = render_completion(_execution(), Verbosity.DEBUG)

        self.assertEqual(
            output,
            "Implementation completed.\n\nFiles changed: 1\nTests: PASS\n"
            "PR: https://github.example/pr/1\n\nFiles changed:\n- A.kt\n\n"
            "Diff:\ndiff --git a/A.kt b/A.kt\n\nLogs:\nverbose output\n\n"
            "Commands:\n- /diff\n- /show <file-number>\n- /logs\n- /pr",
        )
        normal = render_completion(_execution(pr_url=None), Verbosity.NORMAL)
        self.assertIn("Files changed:\n- A.kt", normal)
        self.assertNotIn("verbose output", normal)
        self.assertNotIn("PR:", normal)

    def test_render_plan_full_output(self) -> None:
        plan = PlanState(
            id="abc12345",
            feature="Add login",
            revision=2,
            plan_text=json.dumps(
                {
                    "branch": "feature/add-login",
                    "summary": "Add a login page",
                    "files": ["login.py"],
                    "steps": ["Add form", "Wire route"],
                    "risks": ["Session expiry"],
                }
            ),
            approved=False,
            repository_identity={
                "project_name": "repo",
                "repo_path": "/tmp/repo",
                "github_repository": "owner/repo",
            },
        )

        with patch.object(plan_state, "active_project", return_value=PROJECT):
            output = render_plan(plan)

        self.assertEqual(
            output,
            "Plan #abc12345\nRevision: 2\n\nProject:\nrepo (owner/repo)\n\n"
            "Branch:\nfeature/add-login\n\nSummary:\nAdd a login page\n\n"
            "Files:\n- login.py\n\nImplementation:\n1. Add form\n2. Wire route\n\n"
            "Risks:\n- Session expiry\n\n"
            "Commands:\n- /discuss <feedback>\n- /approve\n- /cancel",
        )

    def test_render_plan_looks_up_the_parser_in_plan_state(self) -> None:
        document = plan_document.PlanDocument(
            branch="feature/patched",
            summary="Patched summary",
            files=[],
            steps=[],
            risks=[],
            codex_prompt="",
        )
        plan = new_plan_state("Feature", "ignored")

        with (
            patch.object(plan_state, "active_project", return_value=PROJECT),
            patch.object(plan_state, "parse_plan_document", return_value=document),
        ):
            output = render_plan(plan)

        self.assertIn("Branch:\nfeature/patched", output)
        self.assertIn("Summary:\nPatched summary", output)

    def test_render_diff_summary_counts_changed_lines(self) -> None:
        diff = "--- a/A.kt\n+++ b/A.kt\n+added\n+again\n-removed"

        self.assertEqual(
            render_diff_summary(diff, ["A.kt"]),
            "Modified files:\n1. A.kt\n\n+2 lines\n-1 lines",
        )
        self.assertEqual(
            render_diff_summary("", []), "Modified files:\n(none)\n\n+0 lines\n-0 lines"
        )

    def test_revisions_keep_history_and_reset_approval(self) -> None:
        plan = new_plan_state("Feature", "v1")
        plan.approved = True

        second = revise_plan_state(plan, "v2")
        third = revise_plan_state(second, "v3")

        self.assertEqual((plan.revision, plan.history), (1, []))
        self.assertEqual(third.id, plan.id)
        self.assertEqual(third.feature, "Feature")
        self.assertEqual(third.revision, 3)
        self.assertEqual(third.history, ["v1", "v2"])
        self.assertEqual(third.plan_text, "v3")
        self.assertFalse(second.approved)
        self.assertEqual(len(plan.id), 8)

    def test_malformed_plans_fall_back_to_plain_text(self) -> None:
        non_object = parse_plan_document('["a", "b"]', "Add login")
        self.assertEqual(non_object.branch, "feature/add-login")
        self.assertEqual(non_object.summary, '["a", "b"]')
        self.assertEqual(non_object.files, [])

        empty = parse_plan_document("", "Add login")
        self.assertEqual(empty.summary, "Add login")
        self.assertEqual(empty.steps, [])

        nothing = parse_plan_document("")
        self.assertEqual(nothing.branch, "feature/change")
        self.assertEqual(nothing.summary, "Planned change")

        bullets = parse_plan_document("- one\n- two\n" + "- more\n" * 10)
        self.assertEqual(bullets.steps[:2], ["one", "two"])
        self.assertEqual(len(bullets.steps), 8)

    def test_plan_fields_are_normalized(self) -> None:
        document = parse_plan_document(
            json.dumps(
                {
                    "branch": "no-prefix",
                    "summary": "  Add login page  ",
                    "files": [" a.py ", 3, "", None],
                    "steps": "not a list",
                }
            )
        )

        self.assertEqual(document.branch, "feature/add-login-page")
        self.assertEqual(document.summary, "Add login page")
        self.assertEqual(document.files, ["a.py"])
        self.assertEqual(document.steps, [])

        invalid_prefix = parse_plan_document(
            json.dumps({"branch": "bad prefix!/x", "summary": "Add login"})
        )
        self.assertEqual(invalid_prefix.branch, "feature/add-login")

    def test_plan_outline_is_bounded(self) -> None:
        plan = new_plan_state(
            "Feature",
            json.dumps(
                {
                    "summary": "s" * 2000,
                    "files": [f"f{n}.py" for n in range(100)],
                    "codex_prompt": "secret prompt",
                }
            ),
        )

        outline = plan_outline(plan)

        self.assertEqual(len(outline["summary"]), plan_state.OUTLINE_ITEM_LENGTH)
        self.assertEqual(len(outline["files"]), plan_state.OUTLINE_ITEMS)
        self.assertNotIn("secret prompt", json.dumps(outline))

    def test_old_plan_state_import_paths_resolve_to_moved_objects(self) -> None:
        for name in ("PlanDocument", "parse_plan_document"):
            with self.subTest(name=name):
                self.assertIn(name, plan_state.__all__)
                self.assertIs(getattr(plan_state, name), getattr(plan_document, name))


if __name__ == "__main__":
    unittest.main()
