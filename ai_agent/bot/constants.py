"""Define command metadata and fleet deployment targets."""

from __future__ import annotations

from pathlib import Path

from telegram import BotCommand

BOT_COMMANDS = [
    BotCommand("start", "Show help"),
    BotCommand("help", "Show help"),
    BotCommand("more", "Full command reference"),
    BotCommand("version", "Show the running agent version"),
    BotCommand("plan", "Create a plan for discussion"),
    BotCommand("discuss", "Revise the current plan"),
    BotCommand("approve", "Approve the current plan"),
    BotCommand("showplan", "Show the current plan"),
    BotCommand("history", "Show plan revisions"),
    BotCommand("implement", "Plan and wait for confirm"),
    BotCommand("bugfix", "Prepare a bugfix branch"),
    BotCommand("answer", "Answer bugfix questions"),
    BotCommand("confirm", "Queue approved work and run tasks"),
    BotCommand("queue", "Show queued work"),
    BotCommand("planner", "Choose Codex or Claude for planning"),
    BotCommand("agent", "Choose Codex or Claude for implementation"),
    BotCommand("fixpr", "Repair failed CI on an existing PR"),
    BotCommand("cancel", "Discard pending work"),
    BotCommand("ci", "Show PR CI status"),
    BotCommand("diff", "Show last changed files"),
    BotCommand("show", "Show one file diff"),
    BotCommand("pr", "Show the last PR URL"),
    BotCommand("logs", "Show logs"),
    BotCommand("verbosity", "Show or set reply verbosity"),
    BotCommand("limits", "Show Codex and Claude limits/status"),
    BotCommand("model", "Show or switch the Claude model"),
    BotCommand("core", "Core version; update <bot> / release <ver> <note> (hub)"),
    BotCommand("codex", "Show Codex status"),
    BotCommand("test", "Run agent unit tests"),
    BotCommand("pull", "git pull the active project"),
    BotCommand("repo_list", "List projects, active marked with *"),
    BotCommand("repo_add", "Register and clone a project"),
    BotCommand("repo_use", "Switch the active project"),
    BotCommand("repo_remove", "Unregister a project"),
    BotCommand("branches", "List branches"),
    BotCommand("branch", "Show or switch the current branch"),
    BotCommand("status", "Show active or git status"),
    BotCommand("deploy", "Deploy a branch of the active project"),
]


DEPLOY_TARGETS = {
    "coding": {
        "label": "ai-coding-agent (self)",
        "script": "/usr/local/sbin/update-ai-agent",
        "log": Path("/var/log/ai-agent/update.log"),
        "repo": Path("/opt/ai-coding-agent"),
        "self": True,
    },
    "pm": {
        "label": "ai-pm-agent",
        "script": "/usr/local/sbin/update-ai-pm-agent",
        "log": Path("/var/log/ai-pm-agent/update.log"),
        "repo": Path("/opt/ai-pm-agent"),
        "self": False,
    },
    "ops": {
        "label": "ai-ops-agent",
        "script": "/usr/local/sbin/update-ai-ops-agent",
        "log": Path("/var/log/ai-ops-agent/update.log"),
        "repo": Path("/opt/ai-ops-agent"),
        "self": False,
    },
}


DEPLOY_TARGET_ALIASES = {
    "coding": "coding",
    "self": "coding",
    "ai-agent": "coding",
    "ai-coding-agent": "coding",
    "pm": "pm",
    "ai-pm-agent": "pm",
    "ops": "ops",
    "ai-ops-agent": "ops",
}
