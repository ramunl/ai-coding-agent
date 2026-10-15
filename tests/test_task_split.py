import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_agent import task_split, tasks, worker
from ai_agent.plan_state import PlanState, parse_plan_document

PLAN = json.dumps(
    {
        "branch": "feature/drag-todos",
        "summary": "Drag todo items in the PM window",
        "files": [],
        "steps": [],
        "risks": [],
        "repositories": [
            {"name": "ai-pm-agent", "changes": "move op in task_cli"},
            {"name": "ai-dashboard", "changes": "drag the PM list"},
        ],
    }
)
KNOWN = ("ai-pm-agent", "ai-dashboard", "ai-coding-agent")


def _plan(text: str = PLAN) -> PlanState:
    return PlanState(
        id="p1", feature="drag todos", revision=1, plan_text=text, approved=False
    )


class SplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data: dict = {}

    def split(self, plan: PlanState | None = None) -> list[dict]:
        plan = plan or _plan()
        return task_split.split_plan(
            self.data, plan, parse_plan_document(plan.plan_text), KNOWN
        )

    def test_one_todo_task_per_repository_in_order_each_after_the_last(self) -> None:
        first, second = self.split()
        self.assertEqual(
            (first["repo"], second["repo"]), ("ai-pm-agent", "ai-dashboard")
        )
        self.assertEqual((first["stage"], second["stage"]), ("todo", "todo"))
        self.assertEqual(first["title"], "drag todos (ai-pm-agent, part 1/2)")
        self.assertEqual((first["after"], second["after"]), (None, first["id"]))
        self.assertEqual({first["group"], second["group"]}, {"p1"})
        self.assertIn("Plan only this repository's part: move op", first["text"])
        self.assertIn("2. ai-dashboard: drag the PM list", first["text"])
        self.assertIn("Whole change: Drag todo items", second["text"])

    def test_the_planned_task_is_replaced_and_its_todo_goes_with_the_last_part(
        self,
    ) -> None:
        source = tasks.add_task(self.data, "drag todos", "ai-pm-agent", "3:abc")
        tasks.set_stage(source, "planned")
        source["plan_id"] = "p1"
        first, second = self.split()
        self.assertNotIn(source, tasks.tasks_of(self.data))
        self.assertEqual((first["todo"], second["todo"]), (None, "3:abc"))

    def test_unknown_or_single_repositories_are_refused(self) -> None:
        data = json.loads(PLAN)
        data["repositories"][1]["name"] = "ai-unknown"
        with self.assertRaisesRegex(
            task_split.SplitError, "Not registered: ai-unknown"
        ):
            self.split(_plan(json.dumps(data)))
        data["repositories"] = [{"name": "ai-pm-agent", "changes": "x"}]
        with self.assertRaisesRegex(task_split.SplitError, "only this repository"):
            task_split.split_plan(
                self.data,
                _plan(json.dumps(data)),
                parse_plan_document(json.dumps(data)),
                KNOWN,
                "ai-pm-agent",
            )
        data["repositories"] = []
        with self.assertRaisesRegex(task_split.SplitError, "only this repository"):
            self.split(_plan(json.dumps(data)))
        self.assertEqual(tasks.tasks_of(self.data), [])

    def test_a_plan_for_another_single_repository_moves_there(self) -> None:
        data = json.loads(PLAN)
        data["repositories"] = [{"name": "ai-dashboard", "changes": "logs scroll"}]
        self.data["pending_plan"] = _plan(json.dumps(data))
        projects = [SimpleNamespace(name=name) for name in KNOWN]
        active = SimpleNamespace(name="ai-coding-agent")
        with (
            patch.object(task_split, "list_projects", return_value=projects),
            patch.object(task_split, "active_project", return_value=active),
        ):
            reply = task_split.split_pending(self.data)
        (task,) = tasks.tasks_of(self.data)
        self.assertEqual((task["repo"], task["stage"]), ("ai-dashboard", "todo"))
        self.assertEqual(task["title"], "drag todos (ai-dashboard)")
        self.assertIsNone(task["after"])
        self.assertIn("In this repository: logs scroll", task["text"])
        self.assertIn("Moved to ai-dashboard", reply)
        self.assertNotIn("pending_plan", self.data)

    def test_a_part_waits_until_the_part_before_has_a_pull_request(self) -> None:
        first, second = self.split()
        self.assertIsNone(tasks.waiting_for(first, self.data))
        self.assertIs(tasks.waiting_for(second, self.data), first)
        self.assertEqual(tasks.view(second, self.data)["waiting_for"], first["title"])
        self.assertEqual(tasks.view(second, self.data)["part"], 2)
        tasks.set_stage(first, "pr")
        self.assertIsNone(tasks.waiting_for(second, self.data))
        tasks.tasks_of(self.data).remove(first)
        self.assertIsNone(tasks.waiting_for(second, self.data))

    def test_the_worker_skips_a_waiting_part(self) -> None:
        first, second = self.split()
        self.data["worker"] = {"enabled": True}
        tasks.set_stage(first, "blocked")
        decision = worker.decide(self.data, tasks.tasks_of(self.data), {}, 0.0)
        self.assertEqual(decision.status, "idle: no To do tasks")
        tasks.set_stage(first, "pr")
        decision = worker.decide(self.data, tasks.tasks_of(self.data), {}, 0.0)
        self.assertEqual(decision.task, second)

    def test_split_pending_drops_the_plan_and_says_what_comes_next(self) -> None:
        self.data.update(pending_plan=_plan(), pending_implementation={"x": 1})
        projects = [SimpleNamespace(name=name) for name in KNOWN]
        active = SimpleNamespace(name="ai-pm-agent")
        with (
            patch.object(task_split, "list_projects", return_value=projects),
            patch.object(task_split, "active_project", return_value=active),
        ):
            reply = task_split.split_pending(self.data)
        self.assertNotIn("pending_plan", self.data)
        self.assertNotIn("pending_implementation", self.data)
        self.assertIn("Split into 2 tasks", reply)
        self.assertIn("1. ai-pm-agent\n2. ai-dashboard", reply)
        with self.assertRaisesRegex(task_split.SplitError, "No pending plan"):
            task_split.split_pending(self.data)
