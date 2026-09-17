"""Structured operational logs; free-form input and exception text are excluded."""

import json
import logging
from datetime import UTC, datetime

FIELDS = (
    "run_id",
    "source_key",
    "record_id",
    "status",
    "duration_ms",
    "items_seen",
    "items_inserted",
    "items_failed",
    "error_category",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": record.msg
            if record.name.startswith("tutor_lead_monitor")
            else "dependency_log",
        }
        for name in FIELDS:
            if hasattr(record, name):
                payload[name] = getattr(record, name)
        # Deliberately omit args, exc_info and arbitrary extras, which can carry secrets.
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
