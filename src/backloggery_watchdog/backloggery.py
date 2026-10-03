from __future__ import annotations

from typing import Any

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
    ):
        self.allow_writes = allow_writes
        self.stealth_save = stealth_save
        self.client = httpx.Client(
            base_url=BACKLOGGERY_BASE,
            timeout=20,
            cookies={"PHPSESSID": php_session_id, "log_token": log_token},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            transport=transport,
        )

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
        try:
            response = self.client.post(endpoint, json=body)
        except httpx.HTTPError as exc:
            raise UpstreamError("Backloggery request failed") from exc
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
