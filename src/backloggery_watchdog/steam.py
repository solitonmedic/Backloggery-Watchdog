from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .errors import AuthenticationError, UpstreamError


class SteamClient:
    """Read-only client for Steam's player library Web API."""

    BASE_URL = "https://api.steampowered.com"

    def __init__(self, api_key: str, steam_id: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None
        self.request_log: list[dict[str, Any]] = []
        self.steam_id = self._resolve_steam_id(steam_id)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _request(
        self, method: str, params: dict[str, Any], *, service_interface: bool = False,
        version: int = 1,
    ) -> dict[str, Any]:
        request_params = {"key": self.api_key, **params}
        if service_interface:
            request_params = {
                "key": self.api_key,
                "format": "json",
                "input_json": json.dumps(params, separators=(",", ":")),
            }
        endpoint = f"{self.BASE_URL}/{method}/v{version}/"
        try:
            response = self.client.get(
                endpoint,
                params=request_params,
            )
        except httpx.HTTPError as exc:
            # Do not include the request URL: it contains the API key.
            raise UpstreamError("Steam Web API request failed") from exc
        if response.status_code in {401, 403}:
            raise AuthenticationError("Steam Web API rejected the configured credentials")
        if response.status_code >= 400:
            raise UpstreamError(f"Steam Web API returned HTTP {response.status_code}")
        self.request_log.append(
            {
                "method": "GET",
                "endpoint": endpoint,
                "request_params": {
                    **{key: value for key, value in request_params.items() if key != "key"},
                    "key": "[REDACTED]",
                },
                "http_status": response.status_code,
            }
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError("Steam Web API returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise UpstreamError("Steam Web API returned an invalid response")
        return data

    def _resolve_steam_id(self, value: str) -> str:
        value = value.strip()
        if re.fullmatch(r"\d{17}", value):
            return value
        parsed = urlparse(value if "://" in value else f"https://{value}")
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if (
            parsed.netloc not in {"steamcommunity.com", "www.steamcommunity.com"}
            or len(parts) != 2
            or parts[0] not in {"id", "profiles"}
        ):
            raise UpstreamError(
                "Steam account must be a 17-digit SteamID64 or a steamcommunity.com profile URL"
            )
        if parts[0] == "profiles" and re.fullmatch(r"\d{17}", parts[1]):
            return parts[1]
        data = self._request(
            "ISteamUser/ResolveVanityURL", {"vanityurl": parts[1], "url_type": 1}
        )
        response = data.get("response")
        if not isinstance(response, dict) or str(response.get("success")) != "1":
            raise UpstreamError("Steam could not resolve the supplied profile URL")
        steam_id = response.get("steamid")
        if not isinstance(steam_id, str) or not re.fullmatch(r"\d{17}", steam_id):
            raise UpstreamError("Steam returned an invalid ID for the supplied profile URL")
        return steam_id

    def owned_games(self) -> list[dict[str, Any]]:
        data = self._request(
            "IPlayerService/GetOwnedGames",
            {
                "steamid": self.steam_id,
                "include_appinfo": True,
                "include_played_free_games": True,
            },
            service_interface=True,
        )
        response = data.get("response")
        if not isinstance(response, dict):
            raise UpstreamError(
                "Steam returned no owned-games list; check the SteamID and profile game-details privacy"
            )
        games = response.get("games")
        if games is None and response.get("game_count") == 0:
            return []
        if not isinstance(games, list):
            raise UpstreamError(
                "Steam returned no owned-games list; check the SteamID and profile game-details privacy"
            )
        if not all(isinstance(game, dict) and game.get("appid") for game in games):
            raise UpstreamError("Steam owned-games response contained malformed game data")
        return games

    def recently_played(self) -> list[dict[str, Any]]:
        data = self._request(
            "IPlayerService/GetRecentlyPlayedGames",
            {"steamid": self.steam_id, "count": 0},
            service_interface=True,
        )
        response = data.get("response")
        if not isinstance(response, dict):
            raise UpstreamError("Steam recently-played response was malformed")
        games = response.get("games", [])
        if not isinstance(games, list):
            raise UpstreamError("Steam recently-played response was malformed")
        if not all(isinstance(game, dict) and game.get("appid") for game in games):
            raise UpstreamError("Steam recently-played response contained malformed game data")
        return games

    def public_game(self, app_id: int) -> dict[str, Any] | None:
        """Resolve one exact AppID in Valve's public game catalog."""
        app_id = int(app_id)
        if app_id < 1 or app_id > 0xFFFFFFFF:
            return None
        data = self._request(
            "IStoreService/GetAppList",
            {"last_appid": app_id - 1, "max_results": 1, "include_games": True},
            service_interface=True,
        )
        response = data.get("response")
        apps = response.get("apps") if isinstance(response, dict) else None
        if not isinstance(apps, list):
            raise UpstreamError("Steam public game catalog response was malformed")
        if not apps or not isinstance(apps[0], dict):
            return None
        game = apps[0]
        try:
            catalog_app_id = int(game.get("appid") or 0)
        except (TypeError, ValueError) as exc:
            raise UpstreamError("Steam public game catalog returned an invalid AppID") from exc
        if catalog_app_id != app_id or not str(game.get("name") or "").strip():
            return None
        return game

    def currently_playing(self) -> int | None:
        """Return the active AppID reported in the player's public summary, if any."""
        data = self._request(
            "ISteamUser/GetPlayerSummaries",
            {"steamids": self.steam_id},
            version=2,
        )
        response = data.get("response")
        players = response.get("players") if isinstance(response, dict) else None
        if not isinstance(players, list):
            raise UpstreamError("Steam player summary response was malformed")
        if not players:
            return None
        player = players[0]
        if not isinstance(player, dict):
            raise UpstreamError("Steam player summary response was malformed")
        raw_app_id = player.get("gameid")
        if raw_app_id in (None, "", 0, "0"):
            return None
        try:
            app_id = int(raw_app_id)
        except (TypeError, ValueError) as exc:
            raise UpstreamError("Steam player summary contained an invalid active AppID") from exc
        return app_id if app_id > 0 else None

    def player_achievements(self, app_id: int) -> list[dict[str, Any]]:
        data = self._request(
            "ISteamUserStats/GetPlayerAchievements",
            {"steamid": self.steam_id, "appid": int(app_id), "l": "english"},
        )
        stats = data.get("playerstats")
        if not isinstance(stats, dict) or stats.get("success") is False:
            raise UpstreamError(f"Steam achievement data is unavailable for AppID {app_id}")
        achievements = stats.get("achievements")
        if not isinstance(achievements, list) or not all(
            isinstance(item, dict) for item in achievements
        ):
            raise UpstreamError(f"Steam achievement data is malformed for AppID {app_id}")
        return achievements


def latest_unlocked_achievements(
    achievements: list[dict[str, Any]], limit: int = 5
) -> list[str]:
    unlocked = []
    for item in achievements:
        if not item.get("achieved"):
            continue
        try:
            unlock_time = int(item.get("unlocktime", 0) or 0)
        except (TypeError, ValueError):
            continue
        if unlock_time <= 0:
            continue
        name = str(item.get("name") or item.get("apiname") or "").strip()
        if name:
            unlocked.append((unlock_time, name))
    unlocked.sort(key=lambda item: item[0], reverse=True)
    return [name for _, name in unlocked[:limit]]


def achievement_counts(achievements: list[dict[str, Any]]) -> tuple[int, int]:
    """Return earned and total achievement counts from Steam's player stats."""
    earned = sum(1 for item in achievements if str(item.get("achieved", 0)) == "1")
    return earned, len(achievements)


def format_steam_notes(
    recent_minutes: int,
    lifetime_minutes: int,
    recent_achievements: list[str] | None,
) -> str:
    def markdown_text(value: str) -> str:
        for char in ("\\", "*", "_", "[", "]", "(", ")", "<", ">", "`"):
            value = value.replace(char, f"\\{char}")
        return value

    achievements = (
        ", ".join(f"*{markdown_text(name)}*" for name in recent_achievements)
        if recent_achievements
        else ("*Unavailable*" if recent_achievements is None else "*None recorded*")
    )
    return "\n".join(
        [
            f"**Recent playtime:** *{int(recent_minutes)} minutes*",
            f"**Lifetime playtime:** *{int(lifetime_minutes)} minutes*",
            f"**Recent achievements:** {achievements}",
        ]
    )
