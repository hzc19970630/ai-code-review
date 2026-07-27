from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

_REDACT_KEYS = {"authorization", "private-token", "x-gitlab-token", "api_key", "token"}


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _REDACT_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "project_id", "review_job_id", "mr_iid", "error_code"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(redact(payload), ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
