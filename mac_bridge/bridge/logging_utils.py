from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

    EXTRA_FIELDS = (
        "event",
        "mode",
        "device_id",
        "session_id",
        "event_id",
        "event_type",
        "status",
        "path",
        "code",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in self.EXTRA_FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def configure_logging(
    name: str,
    log_dir: Path,
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Builds a logger with console + rotating JSON file handlers."""
    logger = logging.getLogger(name)
    logger.setLevel(level.upper())
    logger.propagate = False

    if logger.handlers:
        return logger

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bridge.log"

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level.upper())
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level.upper())
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    return logger

