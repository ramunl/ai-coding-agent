"""Validate configuration and start the coding agent Telegram application."""

import logging
import os

from ai_agent.config import (
    ANTHROPIC_MODEL,
    IMPLEMENTATION_AGENT,
    PLANNING_AGENT,
    REPO_PATH,
    validate_required_config,
)
from ai_agent.telegram_bot import build_application

logger = logging.getLogger(__name__)


def main() -> None:
    """Configure logging and start polling for Telegram updates."""
    validate_required_config()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build_application()
    logger.info(
        "Agent running with repo_path=%s model=%s "
        "planning_agent=%s implementation_agent=%s",
        REPO_PATH,
        ANTHROPIC_MODEL,
        PLANNING_AGENT,
        IMPLEMENTATION_AGENT,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
