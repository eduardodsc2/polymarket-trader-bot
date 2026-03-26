"""Loguru logging configuration.

Call configure_logging() once at application startup.

Log levels used in this project:
  DEBUG    — tick-by-tick data, raw API responses
  INFO     — decisions, trades, fetcher progress
  WARNING  — risk alerts, data anomalies
  ERROR    — recoverable failures (API errors, retries)
  CRITICAL — halt conditions (circuit breaker open, DB down)
"""
from __future__ import annotations

import sys

from loguru import logger


def configure_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure loguru sinks: stderr + rotating file.

    Args:
        level: Minimum log level for stderr output.
        log_dir: Directory for rotating log files.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level:<8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
            "{message}"
        ),
        colorize=True,
    )
    logger.add(
        f"{log_dir}/bot_{{time:YYYY-MM-DD}}.log",
        level="DEBUG",
        rotation="00:00",
        retention="14 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{line} | {message}",
    )
