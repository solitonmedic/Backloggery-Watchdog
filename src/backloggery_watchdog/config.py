from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import ConfigurationError


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"missing required setting: {name}")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 1:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Config:
    ra_api_key: str
    php_session_id: str
    log_token: str
    ra_username: str
    backloggery_username: str
    database_path: str
    offline_poll_seconds: int
    online_poll_seconds: int
    offline_stable_polls: int
    log_level: str
    log_format: str
    dry_run: bool
    priority_decay_days: int = 14
    stealth_save: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        dry_run = _bool("WATCHDOG_DRY_RUN", True)
        log_format = os.environ.get("WATCHDOG_LOG_FORMAT", "plain").strip().lower()
        if log_format not in {"plain", "json"}:
            raise ConfigurationError("WATCHDOG_LOG_FORMAT must be plain or json")
        return cls(
            ra_api_key=_required("RA_API_KEY"),
            php_session_id=_required("PHPSESSID"),
            log_token=_required("log_token"),
            ra_username=_required("RA_USERNAME"),
            backloggery_username=_required("BACKLOGGERY_USERNAME"),
            database_path=os.environ.get("WATCHDOG_DATABASE_PATH", "/data/watchdog.db"),
            offline_poll_seconds=_positive_int("WATCHDOG_OFFLINE_POLL_SECONDS", 900),
            online_poll_seconds=_positive_int("WATCHDOG_ONLINE_POLL_SECONDS", 60),
            offline_stable_polls=_positive_int("WATCHDOG_OFFLINE_STABLE_POLLS", 3),
            priority_decay_days=_positive_int("WATCHDOG_PRIORITY_DECAY_DAYS", 14),
            stealth_save=_bool("WATCHDOG_STEALTH_SAVE", False),
            log_level=os.environ.get("WATCHDOG_LOG_LEVEL", "INFO").upper(),
            log_format=log_format,
            dry_run=dry_run,
        )
