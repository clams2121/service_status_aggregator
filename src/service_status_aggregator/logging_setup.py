"""Plain-text logging to a size-rotated file, plus stderr when useful."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time

from service_status_aggregator.config import LoggingConfig

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
STDERR_ENV_VAR = "SSA_LOG_STDERR"


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"


def setup_logging(cfg: LoggingConfig, *, force_stderr: bool = False) -> logging.Logger:
    """Configure the root logger. Safe to call once per process."""
    root = logging.getLogger()
    root.setLevel(cfg.level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = _UTCFormatter(FORMAT)
    file_handler = logging.handlers.RotatingFileHandler(
        cfg.path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    want_stderr = force_stderr or os.environ.get(STDERR_ENV_VAR) == "1" or sys.stderr.isatty()
    if want_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    # uvicorn is configured with log_config=None so its loggers propagate here.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger("service_status_aggregator")
