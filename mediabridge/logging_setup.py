"""Logging configuration.

Uses GitHub Actions workflow-command prefixes when running inside Actions so
warnings and errors surface in the job summary instead of being buried in the
log body.
"""

from __future__ import annotations

import logging
import os
import sys

IN_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


class ActionsFormatter(logging.Formatter):
    _PREFIX = {
        logging.WARNING: "::warning::",
        logging.ERROR: "::error::",
        logging.CRITICAL: "::error::",
    }

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        prefix = self._PREFIX.get(record.levelno)
        return f"{prefix}{message}" if prefix else message


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)

    if IN_GITHUB_ACTIONS:
        handler.setFormatter(ActionsFormatter("%(message)s"))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # These are chatty at DEBUG and drown out our own output.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

    # urllib3's own retry warnings echo the full request URL, which for a
    # chunked upload means dumping a 1500-character upload token into the log
    # on every transient error. Our fetchers report retries themselves, with
    # the credential redacted.
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
