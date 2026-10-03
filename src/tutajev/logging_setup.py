"""Logging configuration. Logs go to stderr so stdout stays clean for structured output."""

from __future__ import annotations

import logging
import sys

# tutaproxy logs at INFO with Polish messages and occasionally dumps raw API data at DEBUG;
# typesafe_sdk logs request and response bodies (i.e. message content) at DEBUG.
_NOISY_LOGGERS = ("tuta", "aiohttp", "typesafe_sdk", "httpx2")


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )
    noisy_level = logging.DEBUG if level == "DEBUG" else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy_level)
