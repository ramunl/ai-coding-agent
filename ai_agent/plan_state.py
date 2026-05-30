"""Plan state management for discussion workflow.

Stores plan revisions, approval status, and metadata for structured planning.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class PlanState:
    """Represents a single plan with revision history."""

    id: str  # Unique plan identifier (timestamp-based)
    feature: str  # Original feature description
    revision: int  # Current revision number (starts at 1)
    plan_text: str  # Current plan content
    approved: bool = False  # Whether plan is approved
    history: list[str] = field(default_factory=list)  # Previous revision texts
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    verbosity: str = "concise"  # concise, normal, debug

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "PlanState":
        """Create PlanState from dictionary."""
        return PlanState(**data)


class PlanStateStore:
    """Persistent storage for plan states using JSON files."""

    def __init__(self, storage_dir: Optional[Path] = None):
        """Initialize store with optional custom storage directory."""
        self.storage_dir = storage_dir or Path.home() / ".ai_agent" / "plans"
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_plan_path(self, plan_id: str) -> Path:
        """Get file path for a plan."""
        return self.storage_dir / f"{plan_id}.json"

    def save(self, plan: PlanState) -> None:
        """Save plan to disk."""
        plan.updated_at = datetime.now().isoformat()
        path = self._get_plan_path(plan.id)
        path.write_text(json.dumps(plan.to_dict(), indent=2))

    def load(self, plan_id: str) -> Optional[PlanState]:
        """Load plan from disk."""
        path = self._get_plan_path(plan_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return PlanState.from_dict(data)

    def delete(self, plan_id: str) -> None:
        """Delete plan from disk."""
        path = self._get_plan_path(plan_id)
        path.unlink(missing_ok=True)

    def list_plans(self) -> list[str]:
        """List all stored plan IDs."""
        return [p.stem for p in self.storage_dir.glob("*.json")]


class PlanContext:
    """Per-user plan context for the current bot session."""

    def __init__(self, store: Optional[PlanStateStore] = None):
        """Initialize plan context."""
        self.store = store or PlanStateStore()
        self.pending_plan: Optional[PlanState] = None

    def create_plan(self, feature: str, plan_text: str) -> PlanState:
        """Create a new plan."""
        plan_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        plan = PlanState(
            id=plan_id,
            feature=feature,
            revision=1,
            plan_text=plan_text,
        )
        self.pending_plan = plan
        self.store.save(plan)
        return plan

    def revise_plan(self, feedback: str, revised_text: str) -> PlanState:
        """Create a new revision of the current plan."""
        if not self.pending_plan:
            raise ValueError("No pending plan to revise")

        plan = self.pending_plan
        plan.history.append(plan.plan_text)
        plan.revision += 1
        plan.plan_text = revised_text
        plan.updated_at = datetime.now().isoformat()
        self.store.save(plan)
        return plan

    def approve_plan(self) -> PlanState:
        """Mark current plan as approved."""
        if not self.pending_plan:
            raise ValueError("No pending plan to approve")

        plan = self.pending_plan
        plan.approved = True
        self.store.save(plan)
        return plan

    def get_plan_history_titles(self) -> list[str]:
        """Get revision titles (revision numbers only)."""
        if not self.pending_plan:
            return []

        plan = self.pending_plan
        titles = [f"Revision {i}" for i in range(1, plan.revision)]
        if plan.revision > 1:
            titles.append(f"Revision {plan.revision} (current)")
        return titles

    def cancel_plan(self) -> None:
        """Cancel and discard pending plan."""
        if self.pending_plan:
            self.store.delete(self.pending_plan.id)
            self.pending_plan = None

    def set_verbosity(self, level: str) -> None:
        """Set verbosity level for current plan."""
        if self.pending_plan:
            if level not in ("concise", "normal", "debug"):
                raise ValueError(f"Invalid verbosity level: {level}")
            self.pending_plan.verbosity = level
            self.store.save(self.pending_plan)
