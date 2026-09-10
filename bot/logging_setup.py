from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.config import BOT_TOKEN, LOG_DIR, LOG_LEVEL


class _RedactTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        token = BOT_TOKEN
        if not token or token.startswith("123456"):
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if token in message:
            record.msg = message.replace(token, "***TOKEN***")
            record.args = ()
        return True


def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    redact = _RedactTokenFilter()

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(redact)
    root.addHandler(stream)

    if LOG_DIR:
        log_dir = Path(LOG_DIR)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "bot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redact)
            root.addHandler(file_handler)
        except OSError as exc:
            logging.getLogger(__name__).warning("Cannot write log file in %s: %s", log_dir, exc)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(level)

    if LOG_DIR:
        logging.info("Logs: stdout + %s", Path(LOG_DIR) / "bot.log")
    else:
        logging.info("Logs: stdout")
