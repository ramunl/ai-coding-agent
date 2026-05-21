import logging
import os

from ai_agent.config import ANTHROPIC_MODEL, REPO_PATH
from ai_agent.telegram_bot import build_application


logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = build_application()
    logger.info("Agent running with repo_path=%s model=%s", REPO_PATH, ANTHROPIC_MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()
