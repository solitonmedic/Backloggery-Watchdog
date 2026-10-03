from __future__ import annotations

from typing import Any

import httpx

from .errors import AuthenticationError, UpstreamError

BACKLOGGERY_BASE = "https://backloggery.com"


class BackloggeryClient:
    """Read-only connector for the endpoints used by the current frontend."""

    def __init__(
        self,
        php_session_id: str,
        log_token: str,
        transport: httpx.BaseTransport | None = None,
    ):
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
        if endpoint not in {
            "/api/fetch_user_platforms.php",
            "/api/fetch_library.php",
            "/api/fetch_gameinfo.php",
        }:
            raise UpstreamError("Backloggery connector rejected a non-read endpoint")
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
        if (
            endpoint != "/api/fetch_gameinfo.php"
            and isinstance(data, dict)
            and data.get("status") in {0, False}
        ):
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
