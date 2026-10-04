from __future__ import annotations

from typing import Any

from .backloggery import BackloggeryClient
from .errors import UpstreamError, WatchdogError
from .state import StateStore
from .steam import (
    SteamClient,
    achievement_counts,
    format_steam_notes,
    latest_unlocked_achievements,
)

DIGITAL_FORMAT = 1


def _set_linked_pc_entry_digital(
    backloggery: BackloggeryClient,
    entry_id: int,
    platform_id: int,
    *,
    dry_run: bool = False,
) -> bool:
    """Read, preserve, and verify one linked PC entry's Format field."""
    latest = backloggery.game(entry_id)
    if latest is None or int(latest.get("platform_id") or 0) != platform_id:
        raise UpstreamError(f"linked Steam PC entry {entry_id} changed or disappeared")
    raw_format = latest.get("phys_digi")
    if raw_format is None:
        return False
    if int(raw_format) == DIGITAL_FORMAT:
        return False
    if dry_run:
        return True
    payload = dict(latest)
    payload["phys_digi"] = DIGITAL_FORMAT
    payload["prev_status"] = latest.get("status")
    payload["prev_own"] = latest.get("own")
    backloggery.update_game(payload)
    confirmed = backloggery.game(entry_id)
    if confirmed is None or int(confirmed.get("phys_digi") or 0) != DIGITAL_FORMAT:
        raise UpstreamError(f"Digital format update could not be verified for entry {entry_id}")
    return True


def _pc_platform(backloggery: BackloggeryClient) -> dict[str, Any]:
    platforms = backloggery.platforms()
    pc = next((item for item in platforms if str(item.get("title", "")).casefold() == "pc"), None)
    if pc is not None:
        return pc
    matches = [
        item
        for item in backloggery.platform_catalog()
        if str(item.get("title", "")).casefold() == "pc"
    ]
    if len(matches) != 1:
        raise UpstreamError("Backloggery platform catalog did not contain one exact PC match")
    selected = matches[0]
    backloggery.add_platform(selected)
    refreshed = backloggery.platforms()
    registered = next(
        (
            item
            for item in refreshed
            if str(item.get("title", "")).casefold() == "pc"
            and int(item.get("platform_id", 0)) == int(selected["platform_id"])
        ),
        None,
    )
    if registered is None:
        raise UpstreamError("Backloggery did not confirm the new PC platform")
    return registered


def submit_steam_candidate(
    store: StateStore,
    backloggery: BackloggeryClient,
    steam: SteamClient,
    app_id: int,
    username: str,
    *,
    active_session: bool = False,
) -> dict[str, Any]:
    candidate = store.get_steam_candidate(app_id)
    if candidate is None:
        raise ValueError(f"unknown Steam AppID {app_id}")
    if candidate["review_status"] not in {"accepted", "accepted_with_edits"}:
        raise ValueError("Steam candidate must be accepted before it can be submitted")
    linked_id = candidate.get("backloggery_game_inst_id")
    if linked_id is not None:
        return {"appid": app_id, "action": "already_submitted", "game_inst_id": int(linked_id)}

    title = str(candidate.get("canonical_title") or candidate["steam_name"])
    recent_minutes = int(candidate.get("playtime_recent") or 0)
    lifetime_minutes = int(candidate.get("playtime_forever") or 0)
    try:
        stats = steam.player_achievements(app_id)
    except WatchdogError:
        achievements: list[str] = []
        earned = total = None
        achievement_status = "unavailable"
    else:
        achievements = latest_unlocked_achievements(stats)
        earned, total = achievement_counts(stats)
        achievement_status = "available"
    notes = format_steam_notes(
        recent_minutes,
        lifetime_minutes,
        achievements if achievement_status == "available" else None,
    )
    store.set_steam_candidate_details(
        app_id, notes, achievements, achievement_status, earned, total
    )

    status = 10  # Unplayed
    if earned is not None and total is not None and total > 0 and earned >= total:
        status = 40  # Completed
    elif lifetime_minutes > 0 or active_session:
        status = 20  # Unfinished

    platform = _pc_platform(backloggery)
    platform_id = int(platform["platform_id"])
    library = backloggery.library(username)
    matches = [
        item
        for item in library
        if int(item.get("platform_id", 0) or 0) == platform_id
        and str(item.get("title", "")).casefold() == title.casefold()
    ]
    if len(matches) > 1:
        raise UpstreamError(f"multiple PC Backloggery entries match Steam candidate {app_id}")
    if matches:
        existing_id = int(matches[0].get("game_inst_id") or matches[0].get("id") or 0)
        if existing_id < 1:
            raise UpstreamError("matching PC Backloggery entry had no stable entry ID")
        _set_linked_pc_entry_digital(backloggery, existing_id, platform_id)
        store.set_steam_candidate_backloggery_link(app_id, existing_id)
        return {"appid": app_id, "action": "linked_existing", "game_inst_id": existing_id}

    payload: dict[str, Any] = {
        "title": title,
        "platform_id": platform_id,
        "platform_title": "PC",
        "abbr": platform.get("abbr", "PC"),
        "status": status,
        "phys_digi": DIGITAL_FORMAT,
        "own": 5 if candidate.get("access_source") == "family_inferred" else 1,
        "region": 1,  # Region is not inferred from a Steam package.
        "notes": notes,
        "priority": 80 if recent_minutes > 0 or active_session else 40,
    }
    if earned is not None and total is not None:
        payload["achieve_score"] = earned
        payload["achieve_total"] = total
    game_inst_id = backloggery.add_game(payload)
    created = backloggery.game(game_inst_id)
    if created is None:
        raise UpstreamError("Backloggery created the Steam entry but it could not be verified")
    if (
        int(created.get("platform_id", 0) or 0) != platform_id
        or str(created.get("title", "")).casefold() != title.casefold()
        or int(created.get("phys_digi", 0) or 0) != DIGITAL_FORMAT
    ):
        raise UpstreamError("Backloggery created a Steam entry with unexpected title, platform, or format")
    store.set_steam_candidate_backloggery_link(app_id, game_inst_id)
    return {"appid": app_id, "action": "created", "game_inst_id": game_inst_id}


def submit_accepted_steam_candidates(
    store: StateStore,
    backloggery: BackloggeryClient,
    steam: SteamClient,
    username: str,
    *,
    app_id: int | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    if app_id is None:
        candidates = store.list_steam_candidates(limit=limit, status="accepted")
        candidates += store.list_steam_candidates(limit=limit, status="accepted_with_edits")
    else:
        candidate = store.get_steam_candidate(app_id)
        candidates = [candidate] if candidate is not None else []
    if not candidates:
        if app_id is not None:
            raise ValueError(f"unknown or unaccepted Steam AppID {app_id}")
        return []
    results = []
    for candidate in candidates:
        assert candidate is not None
        results.append(
            submit_steam_candidate(store, backloggery, steam, int(candidate["steam_appid"]), username)
        )
    return results


def reconcile_steam_digital_format(
    store: StateStore,
    backloggery: BackloggeryClient,
    username: str,
    *,
    limit: int = 20,
    dry_run: bool = False,
) -> dict[str, int]:
    """Change only the format of linked Steam PC entries, in bounded batches."""
    platforms = backloggery.platforms()
    pc_ids = {
        int(item["platform_id"])
        for item in platforms
        if str(item.get("title", "")).casefold() == "pc"
    }
    if len(pc_ids) != 1:
        raise UpstreamError("Backloggery does not have one registered PC platform")
    pc_id = next(iter(pc_ids))
    linked_ids = {
        int(candidate["backloggery_game_inst_id"])
        for candidate in store.list_steam_candidates(limit=100000)
        if candidate.get("backloggery_game_inst_id") is not None
    }
    library = backloggery.library(username)
    targets = sorted(
        int(row["game_inst_id"])
        for row in library
        if int(row.get("game_inst_id") or 0) in linked_ids
        and int(row.get("platform_id") or 0) == pc_id
        and row.get("phys_digi") is not None
        and int(row["phys_digi"]) != DIGITAL_FORMAT
    )
    updated = already_digital = 0
    for entry_id in targets[:limit]:
        if not _set_linked_pc_entry_digital(
            backloggery, entry_id, pc_id, dry_run=dry_run
        ):
            already_digital += 1
            continue
        updated += 1
    return {
        "linked_pc_entries": len(linked_ids),
        "needing_digital": len(targets),
        "updated" if not dry_run else "planned": updated,
        "already_changed_during_run": already_digital,
        "remaining_estimate": max(0, len(targets) - (updated if not dry_run else 0) - already_digital),
    }
