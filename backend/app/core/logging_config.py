"""Small dependency-free JSON-line logger for Phase 1 runtime and request logs."""

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

from app.core.config import get_settings

_CONFIGURED = False


def configure_logging() -> None:
    """Configure console and rotating-file logging once per server process."""

    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    log_file = settings.resolved_log_directory / "app.log"
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logger = logging.getLogger("b2b_backend")
    logger.setLevel(log_level)
    logger.propagate = False

    formatter = logging.Formatter("%(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    logger.handlers.clear()
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    _CONFIGURED = True


def log_event(level: str, event: str, **fields: Any) -> None:
    """Write one JSON object per line, easy to inspect now and import later."""

    configure_logging()
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    logger = logging.getLogger("b2b_backend")
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(json.dumps(payload, default=str, ensure_ascii=False))
