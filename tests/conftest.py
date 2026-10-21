"""Point the token usage store at a temp file before any test imports config.

Config is read once at import, so this must run before the test modules do.
Without it, tests that reach the real recording code would write under
/var/lib/ai-coding-agent.
"""

import atexit
import os
import shutil
import tempfile

_USAGE_DIR = tempfile.mkdtemp(prefix="token-usage-tests-")
atexit.register(shutil.rmtree, _USAGE_DIR, ignore_errors=True)
os.environ["TOKEN_USAGE_PATH"] = os.path.join(_USAGE_DIR, "token-usage.json")
