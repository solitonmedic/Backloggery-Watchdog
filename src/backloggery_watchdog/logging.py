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


def _poll(value: Any) -> str:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return str(value)
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = redact(record.msg if isinstance(record.msg, dict) else {"message": record.getMessage()})
        event = data.get("event")
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        prefix = f"{timestamp} | {record.levelname} | "
        if event == "service_started":
            mode = "DRY RUN" if data.get("dry_run") else "LIVE"
            return prefix + (
                f"Watchdog started | mode={mode} | "
                f"offline poll={_poll(data.get('offline_poll_seconds'))} | "
                f"online poll={_poll(data.get('online_poll_seconds'))}"
            )
        if event == "sync_plan":
            plan = data.get("plan", {})
            action = str(plan.get("action", "unknown")).upper()
            title = plan.get("title") or "No recent game"
            game_id = plan.get("ra_game_id") or "-"
            summary = data.get("summary") or plan.get("reason", "")
            return prefix + f"Plan {action} | {title} | RA {game_id} | {summary}"
        if event == "sync_write":
            action = str(data.get("action", "write")).upper()
            return prefix + (
                f"{action} complete | {data.get('title')} | RA {data.get('ra_game_id')} | "
                f"Backloggery entry {data.get('backloggery_entry_id')} | mapping saved"
            )
        if event == "write_skipped":
            return prefix + f"Write skipped | {data.get('title')} | {data.get('reason')}"
        if event == "auth_check":
            return prefix + (
                f"Authentication successful | RA user={data.get('ra_user')} | "
                f"Backloggery platforms={data.get('backloggery_platform_count')}"
            )
        if event == "healthcheck":
            state = data.get("service_state", data.get("reason", "unknown"))
            return prefix + f"Health {data.get('status')} | service={state}"
        if event == "cycle_degraded":
            return prefix + f"Cycle degraded | {data.get('code')} | {data.get('message')}"
        if event == "mapping_saved":
            return prefix + (
                f"Mapping saved | RA {data.get('ra_game_id')} -> "
                f"Backloggery {data.get('backloggery_entry_id')}"
            )
        if event == "fatal":
            return prefix + f"Fatal error | {data.get('message')}"
        if "message" in data:
            return prefix + str(data["message"])
        details = " | ".join(f"{key}={value}" for key, value in data.items() if key != "event")
        return prefix + f"{event or 'event'}" + (f" | {details}" if details else "")


def configure_logging(level: str, log_format: str = "plain") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if log_format == "json" else PlainFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
