# back_end/Database/logging_config.py
"""
Logging configuration.

Opt #20: RotatingFileHandler — 10 MB / 7 backups, prevents disk exhaustion.
Opt #42: Structured JSON output via python-json-logger when available.

JSON logs allow precise post-incident queries:
  grep '"lid": 3' logs/phonebox_20250422.jsonl
  jq 'select(.levelname=="WARNING")' logs/*.jsonl

Install for JSON logging:
  pip install python-json-logger

Without python-json-logger falls back to plain text — no behaviour change.
"""

import logging
import logging.handlers
import sys
import os
from datetime import datetime


def setup_logging(log_level=logging.INFO):
    """Configure console (plain text) + rotating file (JSON or text) logging."""

    os.makedirs("logs", exist_ok=True)

    # ── Formatter selection (Opt #42) ────────────────────────────────────────
    _json_available = False
    try:
        from pythonjsonlogger import jsonlogger as _jl
        _json_available = True
    except ImportError:
        _jl = None

    plain_fmt = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if _json_available:
        file_fmt = _jl.JsonFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s %(funcName)s %(lineno)d",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    else:
        file_fmt = plain_fmt

    # ── Console handler ──────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(plain_fmt)
    console.setLevel(log_level)

    # ── Rotating file handler (Opt #20) ──────────────────────────────────────
    ext      = "jsonl" if _json_available else "log"
    log_path = f"logs/phonebox_{datetime.now().strftime('%Y%m%d')}.{ext}"

    file_h = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes    = 10 * 1024 * 1024,  # 10 MB
        backupCount = 7,
        encoding    = "utf-8",
    )
    file_h.setFormatter(file_fmt)
    file_h.setLevel(logging.DEBUG)

    # ── Root logger ──────────────────────────────────────────────────────────
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)
    root.addHandler(console)
    root.addHandler(file_h)

    logging.getLogger(__name__).info(
        "Logging configured",
        extra={
            "json": _json_available,
            "log_path": log_path,
            "opts": "#20 #42",
        }
        if _json_available else {},
    )
    return root