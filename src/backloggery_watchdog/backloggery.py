from __future__ import annotations

import email.utils
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fcntl import LOCK_EX, LOCK_UN, flock

import httpx

from .errors import AuthenticationError, UpstreamError

BACKLOGGERY_BASE = "https://backloggery.com"


class BackloggeryClient:
    def __init__(
        self,
        php_session_id: str,
        log_token: str,
        allow_writes: bool = False,
        transport: httpx.BaseTransport | None = None,
        stealth_save: bool = False,
        shared_rate_limit_path: str | None = None,
    ):
        self.allow_writes = allow_writes
        self.stealth_save = stealth_save
        self.shared_rate_limit_path = shared_rate_limit_path
        self.client = httpx.Client(
            base_url=BACKLOGGERY_BASE,
            timeout=20,
            cookies={"PHPSESSID": php_session_id, "log_token": log_token},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            transport=transport,
        )
        self._last_request_at: float | None = None

    @contextmanager
    def _shared_request_lock(self):
        if self.shared_rate_limit_path is None:
            yield None
            return
        path = Path(self.shared_rate_limit_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        try:
            flock(fd, LOCK_EX)
            yield fd
        finally:
            flock(fd, LOCK_UN)
            os.close(fd)

    def _wait_for_request_slot(self, shared_fd: int | None) -> None:
        if shared_fd is None:
            if self._last_request_at is not None:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < 1.1:
                    time.sleep(1.1 - elapsed)
            return
        os.lseek(shared_fd, 0, os.SEEK_SET)
        raw = os.read(shared_fd, 64).decode("ascii", errors="ignore").strip()
        try:
            last_request_at = float(raw)
        except ValueError:
            last_request_at = 0.0
        wait = 1.1 - (time.time() - last_request_at)
        if wait > 0:
            time.sleep(wait)
        os.lseek(shared_fd, 0, os.SEEK_SET)
        os.ftruncate(shared_fd, 0)
        os.write(shared_fd, str(time.time()).encode("ascii"))
        os.fsync(shared_fd)

    def close(self) -> None:
        self.client.close()

    def _post(self, endpoint: str, body: dict[str, Any]) -> Any:
        read_endpoints = {
            "/api/fetch_platforms.php",
            "/api/fetch_user_platforms.php",
            "/api/fetch_library.php",
            "/api/fetch_gameinfo.php",
        }
        write_endpoints = {
            "/api/add_game.php",
            "/api/update_game.php",
            "/api/add_user_platform.php",
        }
        if endpoint not in read_endpoints and not (self.allow_writes and endpoint in write_endpoints):
            raise UpstreamError("Backloggery connector rejected an unauthorized endpoint")
        response = None
        with self._shared_request_lock() as shared_fd:
            for attempt in range(4):
                self._wait_for_request_slot(shared_fd)
                try:
                    response = self.client.post(endpoint, json=body)
                except httpx.HTTPError as exc:
                    raise UpstreamError("Backloggery request failed") from exc
                self._last_request_at = time.monotonic()
                if response.status_code != 429 or attempt == 3:
                    break
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        retry_at = email.utils.parsedate_to_datetime(retry_after)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=UTC)
                        delay = (retry_at - datetime.now(UTC)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        delay = 5.0 * (2**attempt)
                time.sleep(max(1.1, min(delay, 60.0)))
        assert response is not None
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code in {401, 403} or "text/html" in content_type and response.text.lstrip().startswith("<"):
            raise AuthenticationError("Backloggery session has expired")
        if response.status_code != 200:
            raise UpstreamError(f"Backloggery returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise AuthenticationError("Backloggery session returned a non-JSON response") from exc
        if endpoint in {"/api/fetch_user_platforms.php", "/api/fetch_library.php"} and isinstance(data, dict) and data.get("status") in {0, False}:
            raise AuthenticationError("Backloggery session was rejected")
        return data

    @staticmethod
    def _payload(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            payload = data.get("payload", [])
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
        raise UpstreamError("Backloggery response was malformed")

    def platforms(self) -> list[dict[str, Any]]:
        return self._payload(self._post("/api/fetch_user_platforms.php", {"get_owner_platforms": True}))

    def platform_catalog(self) -> list[dict[str, Any]]:
        return self._payload(self._post("/api/fetch_platforms.php", {}))

    def library(self, username: str) -> list[dict[str, Any]]:
        return self._payload(self._post("/api/fetch_library.php", {"username": username}))

    def game(self, game_inst_id: int) -> dict[str, Any] | None:
        data = self._post("/api/fetch_gameinfo.php", {"game_inst_id": game_inst_id})
        if isinstance(data, dict):
            payload = data.get("payload")
            if isinstance(payload, list):
                return payload[0] if payload and isinstance(payload[0], dict) else None
            if isinstance(payload, dict):
                return payload
        return None

    def add_game(self, payload: dict[str, Any]) -> int:
        body = dict(payload)
        body.update({"is_stealth": self.stealth_save, "update_parent": False})
        body.setdefault("priority", 40)
        body.setdefault("notes", "")
        data = self._post("/api/add_game.php", body)
        try:
            entry_id = int(data.get("payload")) if isinstance(data, dict) else 0
        except (TypeError, ValueError):
            entry_id = 0
        if entry_id < 1:
            raise UpstreamError("Backloggery did not return a new entry ID")
        return entry_id

    def add_platform(self, platform: dict[str, Any]) -> None:
        body = {
            "platform_id": int(platform["platform_id"]),
            "title": str(platform["title"]),
            "abbr": str(platform["abbr"]),
            "format": int(platform["format"]),
        }
        data = self._post("/api/add_user_platform.php", body)
        if not isinstance(data, dict) or data.get("payload") is not True:
            raise UpstreamError("Backloggery rejected the platform registration")

    def update_game(self, payload: dict[str, Any]) -> None:
        body = dict(payload)
        body.setdefault("prev_status", payload.get("status"))
        body.setdefault("prev_own", payload.get("own"))
        body["is_stealth"] = self.stealth_save
        body["update_parent"] = bool(payload.get("update_parent", False))
        data = self._post("/api/update_game.php", body)
        if not isinstance(data, dict) or data.get("status") not in {1, True}:
            raise UpstreamError("Backloggery rejected the entry update")
