from __future__ import annotations

from typing import Any

from .models import GameState, SyncPlan

PLATFORM_MAP = {
    "PlayStation 2": "PlayStation 2",
    "Dreamcast": "Dreamcast",
    "Nintendo DS": "Nintendo DS",
    "Nintendo 64": "Nintendo 64",
    "Game Boy": "Game Boy",
    "Game Boy Color": "Game Boy Color",
    "Game Boy Advance": "Game Boy Advance",
    "Nintendo GameCube": "Nintendo GameCube",
    "PlayStation Portable": "PlayStation Portable",
    "PlayStation Vita": "PlayStation Vita",
    "Nintendo 3DS": "Nintendo 3DS",
    "Wii": "Wii",
    "Wii U": "Wii U",
}

STATUS_CODES = {"Unplayed": 10, "Unfinished": 20, "Beaten": 30, "Completed": 40}
STATUS_NAMES = {value: key for key, value in STATUS_CODES.items()}
REGION_CODES = {"Free": 1, "North America": 2, "Japan": 3, "PAL": 4, "Asia": 5, "China": 6, "Korea": 7, "Brazil": 8}
NUMERIC_FIELDS = {"platform_id", "status", "phys_digi", "own", "region", "achieve_score", "achieve_total"}
REQUEST_ONLY_FIELDS = {"platform_title", "abbr"}


def _id(item: dict[str, Any], *names: str) -> int | None:
    for name in names:
        try:
            if item.get(name) is not None:
                return int(item[name])
        except (TypeError, ValueError):
            continue
    return None


def _platform(state: GameState, platforms: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = PLATFORM_MAP.get(state.console)
    if not wanted:
        return None
    return next((item for item in platforms if item.get("title") == wanted), None)


def _candidate_ids(state: GameState, library: list[dict[str, Any]], platform_id: int) -> list[int]:
    found: list[int] = []
    for item in library:
        item_id = _id(item, "game_inst_id", "id")
        item_platform = _id(item, "platform_id")
        if item_id and item_platform == platform_id and str(item.get("title", "")).casefold() == state.title.casefold():
            found.append(item_id)
    return found


def _managed_values(state: GameState, platform: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "title": state.title,
        "platform_id": int(platform["platform_id"]),
        "platform_title": platform.get("title"),
        "abbr": platform.get("abbr"),
        "status": STATUS_CODES[state.status],
        "phys_digi": 20,
        "own": 1,
        "region": REGION_CODES[state.region],
        "achieve_score": state.earned,
        "achieve_total": state.total,
    }
    if state.rich_presence is not None:
        values["notes"] = state.rich_presence
    return values


def _safe_status(existing: Any, desired: int) -> int | Any:
    try:
        current = int(existing)
    except (TypeError, ValueError):
        return existing if existing is not None else desired
    if current not in STATUS_NAMES:
        return current
    return max(current, desired)


def _equal(key: str, current: Any, desired: Any) -> bool:
    if key in NUMERIC_FIELDS:
        try:
            return int(current) == int(desired)
        except (TypeError, ValueError):
            return False
    return current == desired


def build_plan(
    state: GameState,
    platforms: list[dict[str, Any]],
    library: list[dict[str, Any]],
    mapped_entry_id: int | None,
    existing_entry: dict[str, Any] | None,
) -> SyncPlan:
    platform = _platform(state, platforms)
    if platform is None:
        return SyncPlan("blocked", state.ra_game_id, state.title, f"platform unavailable: {state.console}")
    if state.region is None:
        return SyncPlan("blocked", state.ra_game_id, state.title, "region could not be derived from supported hashes")

    platform_id = int(platform["platform_id"])
    if mapped_entry_id is None:
        candidates = _candidate_ids(state, library, platform_id)
        if candidates:
            return SyncPlan(
                "candidate",
                state.ra_game_id,
                state.title,
                "exact title and platform candidate requires explicit mapping",
                candidate_entry_ids=candidates,
            )
        return SyncPlan(
            "create",
            state.ra_game_id,
            state.title,
            "no confirmed mapping or exact candidate",
            proposed_payload=_managed_values(state, platform),
        )

    if existing_entry is None:
        return SyncPlan(
            "blocked",
            state.ra_game_id,
            state.title,
            "confirmed Backloggery entry could not be retrieved",
            backloggery_entry_id=mapped_entry_id,
        )

    desired = _managed_values(state, platform)
    desired["status"] = _safe_status(existing_entry.get("status"), desired["status"])
    proposed = dict(existing_entry)
    changes: dict[str, dict[str, Any]] = {}
    for key, value in desired.items():
        if key in REQUEST_ONLY_FIELDS:
            continue
        if not _equal(key, existing_entry.get(key), value):
            changes[key] = {"from": existing_entry.get(key), "to": value}
            proposed[key] = value
    return SyncPlan(
        "update" if changes else "noop",
        state.ra_game_id,
        state.title,
        "managed fields differ" if changes else "destination already matches",
        backloggery_entry_id=mapped_entry_id,
        changes=changes,
        proposed_payload=proposed if changes else {},
    )
