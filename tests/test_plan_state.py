import json
import unittest

from ai_agent.plan_state import (
    ExecutionState,
    Verbosity,
    new_plan_state,
    parse_plan_document,
    render_completion,
    render_history,
    render_plan,
    revise_plan_state,
)


class PlanStateTests(unittest.TestCase):
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

    def test_render_plan_and_history_include_revision_data(self) -> None:
        plan = new_plan_state("Feature", json.dumps({"summary": "First", "steps": ["Do it"]}))
        revised = revise_plan_state(plan, json.dumps({"summary": "Second", "steps": ["Do it differently"]}))

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


if __name__ == "__main__":
    unittest.main()
