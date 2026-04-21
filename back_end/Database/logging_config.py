# back_end/Database/logging_config.py
import logging
import logging.handlers
import sys
import os
from datetime import datetime


def setup_logging(log_level=logging.INFO):
    """Configure logging for the database module with rotating file output."""

    os.makedirs('logs', exist_ok=True)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)

    # Optimization #20: RotatingFileHandler — 10 MB per file, keep 7 backups.
    # Prevents unbounded log growth on long-running deployments.
    log_path = f'logs/database_{datetime.now().strftime("%Y%m%d")}.log'
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=7,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    return root_logger