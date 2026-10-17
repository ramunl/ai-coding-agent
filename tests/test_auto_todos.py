import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_agent import auto_todos, tasks, worker
from ai_agent.plan_state import PlanState

REPOS = ("ai-dashboard", "ai-pm-agent", "ai-coding-agent")
ID_A = "a" * 32
ID_B = "b" * 32
ID_C = "c" * 32


def fake_bridge(request: dict) -> dict:
    if "project" not in request:
        projects = [{"name": "my_ai_agents"}, {"name": "ai-pm-agent"}, {"name": "../x"}]
        return {"ok": True, "workspace": {"projects": projects}}
    items = {
        "my_ai_agents": [
            {"id": ID_A, "text": "drag todos #auto #ai-dashboard", "status": "open"},
            {"id": ID_B, "text": "not tagged", "status": "open"},
            {"id": ID_C, "text": "old #AUTO thing", "status": "done"},
        ],
        "ai-pm-agent": [
            {"id": ID_C, "text": "#auto sort by priority", "status": "open"},
        ],
    }[request["project"]]
    return {"ok": True, "workspace": {"items": items}}


class AutoTodoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data: dict = {}

    def test_only_open_tagged_todos_of_every_list_are_read(self) -> None:
        found = auto_todos.tagged_todos(fake_bridge)
        self.assertEqual(
            [(item["project"], item["id"]) for item in found],
            [("my_ai_agents", ID_A), ("ai-pm-agent", ID_C)],
        )
        self.assertEqual(auto_todos.tagged_todos(lambda request: None), [])

    def test_tasks_are_made_once_on_autopilot_in_the_right_repository(self) -> None:
        todos = auto_todos.tagged_todos(fake_bridge)
        made = auto_todos.make_tasks(self.data, todos, REPOS, "ai-coding-agent", 100.0)
        self.assertEqual(
            [(task["repo"], task["title"], task["todo"]) for task in made],
            [
                ("ai-dashboard", "drag todos", f"my_ai_agents:{ID_A}"),
                ("ai-pm-agent", "sort by priority", f"ai-pm-agent:{ID_C}"),
            ],
        )
        self.assertTrue(all(task["autopilot"] for task in made))
        self.assertEqual(made[0]["stage"], "todo")
        tasks.tasks_of(self.data).remove(made[0])  # you removed it: not made again
        again = auto_todos.make_tasks(self.data, todos, REPOS, "x", 200.0)
        self.assertEqual(again, [])

    def test_untagged_repository_falls_back_to_the_active_project(self) -> None:
        todo = {"project": "my_ai_agents", "id": ID_A, "text": "fix it #auto"}
        self.assertEqual(
            auto_todos.repository_for(todo, REPOS, "ai-coding-agent"),
            "ai-coding-agent",
        )
        self.assertEqual(
            auto_todos.task_text("fix #auto it #ai-pm-agent #urgent", REPOS),
            "fix it #urgent",
        )

    def test_scans_are_spaced(self) -> None:
        self.assertTrue(auto_todos.is_due(self.data, 1000.0))
        auto_todos.make_tasks(self.data, [], REPOS, "x", 1000.0)
        self.assertFalse(auto_todos.is_due(self.data, 1000.0 + 60))
        self.assertTrue(auto_todos.is_due(self.data, 1000.0 + auto_todos.SCAN_EVERY))


class AutopilotSplitTests(unittest.TestCase):
    def test_an_autopilot_plan_for_other_repositories_is_split(self) -> None:
        data: dict = {"worker": {"enabled": True}}
        task = tasks.add_task(data, "drag todos", "ai-coding-agent", None)
        task.update(stage="planned", plan_id="p1", autopilot=True)
        plan_text = json.dumps(
            {"summary": "x", "repositories": [{"name": "ai-dashboard", "changes": "y"}]}
        )
        data["pending_plan"] = PlanState(
            id="p1",
            feature="drag todos",
            revision=1,
            plan_text=plan_text,
            approved=False,
        )
        active = SimpleNamespace(name="ai-coding-agent")
        with patch.object(worker, "active_project", return_value=active):
            decision = worker.decide(data, tasks.tasks_of(data), {}, 0.0)
        self.assertEqual(decision.action, "split")
        with patch.object(
            worker, "active_project", return_value=SimpleNamespace(name="ai-dashboard")
        ):
            self.assertEqual(
                worker.decide(data, tasks.tasks_of(data), {}, 0.0).action, "approve"
            )
