"""One shared logger factory so every module gets consistent, leveled output
instead of ad hoc print() calls (which also can't be filtered or piped
separately from real errors)."""

import logging
import os

_CONFIGURED = False


def get_logger(name):
    global _CONFIGURED
    if not _CONFIGURED:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        )
        _CONFIGURED = True
    return logging.getLogger(name)
