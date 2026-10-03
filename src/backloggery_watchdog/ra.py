from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from .errors import AuthenticationError, UpstreamError
from .models import GameState

RA_API_BASE = "https://retroachievements.org/API"


class RetroAchievementsClient:
    def __init__(self, api_key: str, username: str, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.username = username
        self.client = httpx.Client(base_url=RA_API_BASE, timeout=20, transport=transport)

    def close(self) -> None:
        self.client.close()

    def _get(self, endpoint: str, params: dict[str, Any]) -> Any:
        request_params = {"z": self.username, "y": self.api_key, **params}
        try:
            response = self.client.get(endpoint, params=request_params)
        except httpx.HTTPError as exc:
            raise UpstreamError("RetroAchievements request failed") from exc
        if response.status_code in {401, 403}:
            raise AuthenticationError("RetroAchievements authentication failed")
        if response.status_code != 200:
            raise UpstreamError(f"RetroAchievements returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError("RetroAchievements returned an invalid response") from exc
        if isinstance(data, dict) and data.get("Error"):
            message = str(data["Error"]).lower()
            if "key" in message or "credential" in message or "author" in message:
                raise AuthenticationError("RetroAchievements authentication failed")
            raise UpstreamError("RetroAchievements rejected the request")
        return data

    def profile(self) -> dict[str, Any]:
        data = self._get("/API_GetUserProfile.php", {"u": self.username})
        if not isinstance(data, dict) or not data.get("User"):
            raise AuthenticationError("RetroAchievements authentication failed")
        return data

    def recently_played(self) -> dict[str, Any] | None:
        data = self._get("/API_GetUserRecentlyPlayedGames.php", {"u": self.username, "c": 1, "o": 0})
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            rows = data.get("Results") or data.get("items") or []
            return rows[0] if rows else None
        raise UpstreamError("RetroAchievements recently played response was malformed")

    def summary(self, game_id: int) -> dict[str, Any]:
        data = self._get("/API_GetUserSummary.php", {"u": self.username, "g": game_id, "a": 5})
        if not isinstance(data, dict):
            raise UpstreamError("RetroAchievements summary response was malformed")
        return data

    def game_progress(self, game_id: int) -> dict[str, Any]:
        data = self._get(
            "/API_GetGameInfoAndUserProgress.php",
            {"u": self.username, "g": game_id, "a": 1},
        )
        if not isinstance(data, dict):
            raise UpstreamError("RetroAchievements progress response was malformed")
        return data

    def game_hashes(self, game_id: int) -> Any:
        return self._get("/API_GetGameHashes.php", {"i": game_id})


REGION_ALIASES: tuple[tuple[str, str, int], ...] = (
    ("north america", "North America", 1),
    ("united states", "North America", 1),
    ("usa", "North America", 1),
    ("u.s.", "North America", 1),
    ("europe", "PAL", 2),
    ("pal", "PAL", 2),
    ("united kingdom", "PAL", 2),
    ("germany", "PAL", 2),
    ("france", "PAL", 2),
    ("spain", "PAL", 2),
    ("italy", "PAL", 2),
    ("australia", "PAL", 2),
    ("japanese", "Japan", 3),
    ("japan", "Japan", 3),
    ("korea", "Korea", 4),
    ("china", "China", 4),
    ("asia", "Asia", 4),
    ("brazil", "Brazil", 4),
)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.lower() not in {"md5", "hash"}:
                yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def derive_region(hashes: Any) -> str | None:
    matches: list[tuple[int, int, str]] = []
    for position, text in enumerate(_strings(hashes)):
        lowered = text.lower()
        for alias, normalized, priority in REGION_ALIASES:
            if alias in lowered:
                matches.append((priority, position, normalized))
    return min(matches)[2] if matches else None


def _int_value(*values: Any) -> int:
    for value in values:
        if value is not None and value != "":
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _achievements(progress: dict[str, Any]) -> list[dict[str, Any]]:
    raw = progress.get("Achievements") or progress.get("achievements") or {}
    if isinstance(raw, dict):
        return [item for item in raw.values() if isinstance(item, dict)]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _earned(value: Any) -> bool:
    return value not in {None, "", "0000-00-00 00:00:00"}


def normalize_game_state(
    recent: dict[str, Any], summary: dict[str, Any], progress: dict[str, Any], hashes: Any
) -> GameState:
    game_id = _int_value(recent.get("GameID"), recent.get("game_id"), recent.get("ID"))
    if not game_id:
        raise UpstreamError("RetroAchievements newest game had no Game ID")
    summary_game_id = _int_value(summary.get("LastGameID"), summary.get("last_game_id"))
    rich_presence = summary.get("RichPresenceMsg") if summary_game_id == game_id else None
    if rich_presence is not None:
        rich_presence = str(rich_presence)

    earned = _int_value(
        progress.get("NumAchieved"),
        progress.get("NumAchievementsEarned"),
        recent.get("NumAchieved"),
    )
    total = _int_value(
        progress.get("NumAchievements"),
        recent.get("NumPossibleAchievements"),
        recent.get("NumAchievements"),
    )
    completion = str(progress.get("UserCompletion") or progress.get("HighestAwardKind") or "").lower()
    hardcore_earned = _int_value(progress.get("NumAchievedHardcore"))
    mastered = completion == "mastered" or (total > 0 and hardcore_earned == total)
    beaten = any(
        str(item.get("Type") or item.get("type") or "").lower() == "win_condition"
        and any(_earned(item.get(key)) for key in ("DateEarnedHardcore", "DateEarned", "dateEarned"))
        for item in _achievements(progress)
    )

    return GameState(
        ra_game_id=game_id,
        title=str(recent.get("Title") or progress.get("Title") or "").strip(),
        console=str(recent.get("ConsoleName") or progress.get("ConsoleName") or "").strip(),
        last_played=str(recent.get("LastPlayed") or ""),
        rich_presence=rich_presence,
        online=str(summary.get("Status") or "").lower() == "online",
        earned=earned,
        total=total,
        beaten=beaten,
        mastered=mastered,
        region=derive_region(hashes),
    )
