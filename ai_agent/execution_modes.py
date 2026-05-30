"""Quiet execution mode and verbosity control."""

from dataclasses import dataclass


@dataclass
class ExecutionResult:
    """Result of code execution with structured output."""

    status: str  # "running", "success", "error"
    files_changed: int = 0
    tests_passed: bool = False
    pr_url: str = ""
    branch_name: str = ""
    error_message: str = ""
    codex_output: str = ""  # Full output (debug only)
    diff_summary: str = ""  # Summary of changes


def format_result_concise(result: ExecutionResult) -> str:
    """Format execution result for concise display.

    Hides all code and diffs by default.
    """
    if result.status == "running":
        return f"Implementation started.\n\nBranch:\n{result.branch_name}"

    elif result.status == "success":
        msg = "Implementation completed.\n\n"
        msg += f"Files changed: {result.files_changed}\n"
        msg += f"Tests: {'PASS' if result.tests_passed else 'RUN'}\n"
        if result.pr_url:
            msg += f"PR: {result.pr_url}"
        msg += "\n\nCommands:\n"
        msg += "/diff - show changed files\n"
        msg += "/show 1 - show specific file diff\n"
        msg += "/logs - show execution logs\n"
        msg += "/pr - show PR details"
        return msg

    else:  # error
        return f"Error:\n{result.error_message}\n\nUse /logs to see details."


def format_result_normal(result: ExecutionResult) -> str:
    """Format execution result for normal display.

    Shows summary but hides raw code.
    """
    if result.status == "running":
        return f"Implementation started.\n\nBranch:\n{result.branch_name}"

    elif result.status == "success":
        msg = "Implementation completed.\n\n"
        msg += f"Files changed: {result.files_changed}\n"
        if result.diff_summary:
            msg += f"\n{result.diff_summary}\n"
        msg += f"\nTests: {'PASS' if result.tests_passed else 'RUN'}\n"
        if result.pr_url:
            msg += f"PR: {result.pr_url}"
        return msg

    else:  # error
        return f"Error:\n{result.error_message}\n\nUse /logs to see details."


def format_result_debug(result: ExecutionResult) -> str:
    """Format execution result for debug display.

    Shows everything including full code and logs.
    """
    msg = f"Status: {result.status}\n"
    msg += f"Branch: {result.branch_name}\n"
    msg += f"Files changed: {result.files_changed}\n"
    msg += f"Tests: {'PASS' if result.tests_passed else 'RUN'}\n"

    if result.codex_output:
        msg += f"\nCodex output:\n{result.codex_output}\n"

    if result.diff_summary:
        msg += f"\nChanges:\n{result.diff_summary}\n"

    if result.error_message:
        msg += f"\nError:\n{result.error_message}\n"

    if result.pr_url:
        msg += f"\nPR: {result.pr_url}"

    return msg


def format_result(result: ExecutionResult, verbosity: str = "concise") -> str:
    """Format execution result based on verbosity level."""
    if verbosity == "concise":
        return format_result_concise(result)
    elif verbosity == "normal":
        return format_result_normal(result)
    else:  # debug
        return format_result_debug(result)
