from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRET_KEYS = {"ra_api_key", "php_session_id", "phpsessid", "log_token", "cookie", "authorization", "y"}
_SECRET_PATTERN = re.compile(
    r"(?i)(RA_API_KEY|PHPSESSID|log_token|Cookie|Authorization|[?&]y)=([^&\s]+)"
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[redacted]" if key.lower() in _SECRET_KEYS else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.msg if isinstance(record.msg, dict) else {"message": record.getMessage()}
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            **redact(message),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
