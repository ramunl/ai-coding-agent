"""Point the token usage store at a temp file before any test imports config.

Config is read once at import, so this must run before the test modules do.
Without it, tests that reach the real recording code would write under
/var/lib/ai-coding-agent.
"""

import atexit
import os
import shutil
import tempfile

# Reproduce scripts/baseline.sh's clean environment so an ambient shell that
# happens to carry live provider/auth settings cannot skew the offline tests.
for _name in (
    "PLANNING_AGENT",
    "IMPLEMENTATION_AGENT",
    "CLAUDE_CODE_AUTH",
    "CLAUDE_PLANNER_VIA",
    "CLAUDE_PLANNER_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "DEEPSEEK_API_KEY",
    "GITHUB_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "YOUR_CHAT_ID",
):
    os.environ.pop(_name, None)

_USAGE_DIR = tempfile.mkdtemp(prefix="token-usage-tests-")
atexit.register(shutil.rmtree, _USAGE_DIR, ignore_errors=True)
os.environ["TOKEN_USAGE_PATH"] = os.path.join(_USAGE_DIR, "token-usage.json")
os.environ["USAGE_DETAILS_PATH"] = os.path.join(_USAGE_DIR, "usage-details.json")

os.environ["CONTEXT_ROOT"] = os.path.join(_USAGE_DIR, "context")

# DS12: provider sandboxes must never touch the real /var/lib in tests.
os.environ["WORK_ROOT"] = os.path.join(_USAGE_DIR, "work")
os.environ["WORKER_HOME"] = os.path.join(_USAGE_DIR, "home")
