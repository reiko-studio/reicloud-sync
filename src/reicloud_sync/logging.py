"""Sysentropy console logging, kept separate from command output."""

import logging
import sys

from sysentropy import LoggerConfig, get_logger


def get_cli_logger() -> logging.Logger:
    logger = get_logger(
        "reicloud",
        config=LoggerConfig(use_colors=sys.stderr.isatty()),
    )
    # Rebind on each invocation, including embedded calls with redirected streams.
    for handler in logger.handlers:
        if handler.name == "sysentropy.stdout":
            handler.stream = sys.stderr
    return logger
