"""Logging setup for fb-scraper.

Writes to both a timestamped file under ``logs/`` and the console.
All scraper modules obtain child loggers via :func:`get_logger`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

_LOGGER_NAME = "fb-scraper"
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_configured = False


def setup_logging(
    log_dir: str = "logs",
    console_level: int = logging.INFO,
) -> logging.Logger:
    """Configure and return the root ``fb-scraper`` logger.

    - Creates ``log_dir`` if it does not exist.
    - Adds a file handler: ``logs/scrape_<YYYYmmdd_HHMMSS>.log`` (DEBUG level).
    - Adds a console handler at ``console_level``.
    - Idempotent: repeated calls reuse the already configured logger.
    """
    global _configured

    logger = logging.getLogger(_LOGGER_NAME)
    if _configured and logger.handlers:
        return logger

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / f"scrape_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logger.setLevel(logging.DEBUG)
    # Keep handlers local; do not propagate duplicate records to root logger.
    logger.propagate = False

    fmt = logging.Formatter(_LOG_FORMAT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    _configured = True

    logger.info("Logging configured. Log file: %s", log_file)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of the ``fb-scraper`` logger, e.g. ``fb-scraper.io_utils``."""
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
