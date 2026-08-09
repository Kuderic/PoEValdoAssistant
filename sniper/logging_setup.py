"""Structured JSON-lines logging via QueueHandler so the hot path never
touches file I/O. One event per line in logs/sniper-YYYYMMDD.jsonl."""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
from datetime import UTC, datetime
from pathlib import Path

from sniper.config import LoggingConfig

_LOGGER_NAME = "sniper"


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        brief = " ".join(f"{k}={v}" for k, v in fields.items() if k != "raw")
        return f"{datetime.now().strftime('%H:%M:%S')} {record.getMessage():<18} {brief}"


def setup(config: LoggingConfig) -> logging.handlers.QueueListener:
    log_dir = Path(config.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(
        log_dir / f"sniper-{datetime.now().strftime('%Y%m%d')}.jsonl", encoding="utf-8"
    )
    file_handler.setFormatter(JsonLinesFormatter())
    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())

    q: queue.Queue = queue.Queue()
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(logging.handlers.QueueHandler(q))

    listener = logging.handlers.QueueListener(q, file_handler, console, respect_handler_level=False)
    listener.start()
    return listener


def event(name: str, **fields) -> None:
    """Log one structured event. Cheap enough for the hot path (queue put)."""
    logging.getLogger(_LOGGER_NAME).info(name, extra={"fields": fields})
