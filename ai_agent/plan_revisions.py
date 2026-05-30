"""Plan discussion and revision logic."""

import anthropic

from ai_agent.config import ANTHROPIC_KEY, ANTHROPIC_MODEL
from ai_agent.plan_state import PlanState


client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def revise_plan_with_feedback(
    current_plan: PlanState, user_feedback: str
) -> str:
    """Revise a plan based on user feedback.

    Args:
        current_plan: The current plan state
        user_feedback: User's feedback for revision

    Returns:
        Revised plan text
    """
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": f"""You are a senior Android architect revising an implementation plan.

Original feature: {current_plan.feature}

Current plan (Revision {current_plan.revision}):
{current_plan.plan_text}

User feedback for revision:
{user_feedback}

Produce a revised plan that addresses the user's feedback while keeping the same structure:
1. Branch name
2. Files to create/modify
3. Step by step implementation plan
4. Ready-to-use Codex prompt

Keep all sections concise and focused.""",
            }
        ],
    )
    return response.content[0].text


def format_plan_for_display(plan: PlanState, verbosity: str = "concise") -> str:
    """Format plan for Telegram display.

    Args:
        plan: The plan to display
        verbosity: Display verbosity level

    Returns:
        Formatted plan text
    """
    if verbosity == "concise":
        # Extract just the key info
        lines = plan.plan_text.split("\n")
        summary = []
        for line in lines:
            if any(
                keyword in line.lower()
                for keyword in ["branch", "file", "step", "risk"]
            ):
                summary.append(line)
            if len(summary) >= 15:  # Limit output
                break
        return "\n".join(summary) if summary else plan.plan_text[:500]

    elif verbosity == "normal":
        # Show more but still abbreviated
        return plan.plan_text[:1500]

    else:  # debug
        # Show everything
        return plan.plan_text


def format_plan_status(plan: PlanState) -> str:
    """Format current plan status for display.

    Returns:
        Status summary string
    """
    status = f"Plan #{plan.id}\n"
    status += f"Revision: {plan.revision}\n"
    status += f"Status: {'✅ Approved' if plan.approved else '⏳ Pending'}\n"
    status += f"Feature: {plan.feature}\n"
    status += f"Branch: feature/..."

    if plan.revision > 1:
        status += f"\nRevisions: {plan.revision} versions"

    return status
