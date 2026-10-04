from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .backloggery import BackloggeryClient
from .config import Config
from .errors import AuthenticationError, UpstreamError
from .models import GameState, SyncPlan
from .planner import REGION_CODES, STATUS_NAMES, build_plan, resolve_platform_title
from .ra import RetroAchievementsClient, normalize_game_state
from .state import StateStore
from .steam import SteamClient, achievement_counts, format_steam_notes, latest_unlocked_achievements
from .steam_submit import submit_steam_candidate

logger = logging.getLogger(__name__)

PRIORITY_NOW_PLAYING = 80
PRIORITY_PAUSED = 60
PRIORITY_HIGH = 50
PRIORITY_NORMAL = 40
PRIORITY_LOW = 30
PRIORITY_NAMES = {
    PRIORITY_NOW_PLAYING: "Now Playing",
    70: "Ongoing",
    PRIORITY_PAUSED: "Paused",
    PRIORITY_HIGH: "High",
    PRIORITY_NORMAL: "Normal",
    PRIORITY_LOW: "Low",
    20: "Replay",
    10: "Shelved",
}


@dataclass(frozen=True, slots=True)
class CycleResult:
    plan: SyncPlan
    active_watch: bool
    stable_polls: int


class WatchdogService:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        ra: RetroAchievementsClient,
        backloggery: BackloggeryClient,
        steam: SteamClient | None = None,
    ):
        self.config = config
        self.store = store
        self.ra = ra
        self.backloggery = backloggery
        self.steam = steam

    @staticmethod
    def _summary(plan: SyncPlan) -> str:
        payload = plan.proposed_payload
        if plan.action in {"create", "update"}:
            status = STATUS_NAMES.get(payload.get("status"), payload.get("status"))
            region = next(
                (name for name, code in REGION_CODES.items() if code == payload.get("region")),
                payload.get("region"),
            )
            notes = "notes=yes" if "notes" in payload else "notes=preserved"
            return (
                f"{payload.get('platform_title', 'unknown platform')} | {status} | "
                f"{payload.get('achieve_score')}/{payload.get('achieve_total')} achievements | "
                f"{region} | Own | Physical | {notes}"
            )
        return plan.reason

    def _apply_live_plan(
        self,
        state: GameState,
        platforms: list[dict],
        plan: SyncPlan,
        field_overrides: dict[str, object] | None = None,
    ) -> SyncPlan:
        if plan.action == "create":
            current_mapping = self.store.get_mapping(state.ra_game_id)
            fresh_platforms = self.backloggery.platforms()
            fresh_library = self.backloggery.library(self.config.backloggery_username)
            refreshed = build_plan(state, fresh_platforms, fresh_library, current_mapping, None)
            if refreshed.action != "create":
                logger.warning(
                    {
                        "event": "write_skipped",
                        "title": state.title,
                        "reason": f"pre-write refresh changed action to {refreshed.action}",
                    }
                )
                return refreshed
            create_payload = dict(refreshed.proposed_payload)
            if state.online:
                create_payload["priority"] = PRIORITY_NOW_PLAYING
            entry_id = self.backloggery.add_game(create_payload)
            self.store.set_mapping(state.ra_game_id, entry_id)
            confirmed = self.backloggery.game(entry_id)
            if confirmed is None:
                raise UpstreamError("Backloggery created an entry but it could not be verified")
            if confirmed is not None and state.region is not None:
                self.store.record_observed_fields(
                    state.ra_game_id,
                    {"title": confirmed.get("title"), "region": confirmed.get("region")},
                )
            verification = build_plan(state, fresh_platforms, [], entry_id, confirmed)
            if verification.action == "update":
                raise UpstreamError("Backloggery created an entry with unexpected managed fields")
            logger.info(
                {
                    "event": "sync_write",
                    "action": "create",
                    "title": state.title,
                    "ra_game_id": state.ra_game_id,
                    "backloggery_entry_id": entry_id,
                }
            )
            refreshed.backloggery_entry_id = entry_id
            return refreshed
        if plan.action == "update" and plan.backloggery_entry_id is not None:
            latest = self.backloggery.game(plan.backloggery_entry_id)
            if latest is not None and state.region is not None:
                field_overrides = self.store.reconcile_field_overrides(
                    state.ra_game_id,
                    {"title": latest.get("title"), "region": latest.get("region")},
                    {"title": state.title, "region": REGION_CODES[state.region]},
                )
            refreshed = build_plan(
                state,
                platforms,
                self.backloggery.library(self.config.backloggery_username),
                plan.backloggery_entry_id,
                latest,
                field_overrides,
            )
            if refreshed.action != "update":
                logger.warning(
                    {
                        "event": "write_skipped",
                        "title": state.title,
                        "reason": f"pre-write refresh changed action to {refreshed.action}",
                    }
                )
                return refreshed
            write_payload = dict(refreshed.proposed_payload)
            write_payload["prev_status"] = latest.get("status") if latest else None
            write_payload["prev_own"] = latest.get("own") if latest else None
            self.backloggery.update_game(write_payload)
            confirmed = self.backloggery.game(plan.backloggery_entry_id)
            if confirmed is None:
                raise UpstreamError("Backloggery updated an entry but it could not be verified")
            verification = build_plan(
                state,
                platforms,
                [],
                plan.backloggery_entry_id,
                confirmed,
                field_overrides,
            )
            if verification.action == "update":
                raise UpstreamError("Backloggery update did not reach the requested managed state")
            if confirmed is not None:
                self.store.record_observed_fields(
                    state.ra_game_id,
                    {"title": confirmed.get("title"), "region": confirmed.get("region")},
                )
            logger.info(
                {
                    "event": "sync_write",
                    "action": "update",
                    "title": state.title,
                    "ra_game_id": state.ra_game_id,
                    "backloggery_entry_id": plan.backloggery_entry_id,
                }
            )
            return refreshed
        return plan

    def _sync_priority_lifecycle(self, state: GameState | None, watch_active: bool = False) -> None:
        """Apply priority aging only to games previously observed Online."""
        tracked = self.store.priority_tracking()
        now = datetime.now(UTC)
        now_text = now.isoformat()
        current_id = state.ra_game_id if state is not None else None

        # A newly reported RA game ends the previous game's Now Playing period.
        for record in tracked:
            ra_game_id = int(record["ra_game_id"])
            if bool(record["online"]) and current_id is not None and ra_game_id != current_id:
                self.store.set_priority_tracking(
                    ra_game_id,
                    str(record["last_fingerprint"]),
                    now_text,
                    False,
                    bool(record["mastered"]),
                )

        if state is not None:
            mapping = self.store.get_mapping(state.ra_game_id)
            existing_track = self.store.get_priority_tracking(state.ra_game_id)
            if mapping is not None and (state.online or watch_active or existing_track is not None):
                if state.online or watch_active:
                    offline_since = None
                    online = True
                else:
                    changed = (
                        existing_track is None
                        or str(existing_track["last_fingerprint"]) != state.fingerprint
                    )
                    offline_since = (
                        now_text
                        if existing_track is None or bool(existing_track["online"]) or changed
                        else existing_track["offline_since"]
                    )
                    online = False
                self.store.set_priority_tracking(
                    state.ra_game_id,
                    state.fingerprint,
                    str(offline_since) if offline_since is not None else None,
                    online,
                    state.mastered,
                )

        for record in self.store.priority_tracking():
            ra_game_id = int(record["ra_game_id"])
            entry_id = self.store.get_mapping(ra_game_id)
            if entry_id is None:
                continue
            if bool(record["online"]):
                desired = PRIORITY_NOW_PLAYING
                reason = "RA reports Online or its Offline confirmation window is still open"
            else:
                offline_since = record["offline_since"]
                if not offline_since:
                    continue
                elapsed = now - datetime.fromisoformat(str(offline_since)).astimezone(UTC)
                step = timedelta(days=self.config.priority_decay_days)
                mastered = bool(record["mastered"])
                if elapsed >= step * 3 and not mastered:
                    desired = PRIORITY_LOW
                    reason = "no RA updates for three priority intervals"
                elif elapsed >= step * 2 and mastered:
                    desired = PRIORITY_NORMAL
                    reason = "mastered game returns to Normal after two priority intervals"
                elif elapsed >= step * 2:
                    desired = PRIORITY_HIGH
                    reason = "no RA updates for two priority intervals"
                elif elapsed >= step:
                    desired = PRIORITY_PAUSED
                    reason = "no RA updates for one priority interval"
                else:
                    continue

            latest = self.backloggery.game(entry_id)
            if latest is None:
                logger.warning(
                    {
                        "event": "priority_sync_skipped",
                        "ra_game_id": ra_game_id,
                        "reason": "mapped entry not found",
                    }
                )
                continue
            try:
                current_priority = int(latest.get("priority", PRIORITY_NORMAL))
            except (TypeError, ValueError):
                current_priority = PRIORITY_NORMAL
            if current_priority == desired:
                continue
            if not bool(record["online"]) and current_priority not in {
                PRIORITY_NOW_PLAYING,
                PRIORITY_PAUSED,
                PRIORITY_HIGH,
                PRIORITY_NORMAL,
                PRIORITY_LOW,
            }:
                # Preserve manually selected Ongoing, Replay, and Shelved priorities.
                continue
            event = {
                "event": "priority_plan",
                "dry_run": self.config.dry_run,
                "ra_game_id": ra_game_id,
                "backloggery_entry_id": entry_id,
                "title": latest.get("title"),
                "from": PRIORITY_NAMES.get(current_priority, str(current_priority)),
                "to": PRIORITY_NAMES[desired],
                "reason": reason,
            }
            if self.config.dry_run:
                logger.info(event)
                continue
            payload = dict(latest)
            payload["priority"] = desired
            payload["prev_status"] = latest.get("status")
            payload["prev_own"] = latest.get("own")
            self.backloggery.update_game(payload)
            confirmed = self.backloggery.game(entry_id)
            try:
                confirmed_priority = int(confirmed.get("priority", -1)) if confirmed else -1
            except (TypeError, ValueError):
                confirmed_priority = -1
            if confirmed_priority != desired:
                raise UpstreamError("Backloggery priority update could not be verified")
            event["event"] = "priority_updated"
            logger.info(event)

    def _ensure_platform(
        self,
        state: GameState,
        platforms: list[dict],
    ) -> tuple[list[dict], SyncPlan | None]:
        wanted = resolve_platform_title(state.console, state.region)
        if wanted is None:
            return platforms, SyncPlan(
                "blocked",
                state.ra_game_id,
                state.title,
                f"no Backloggery platform mapping for {state.console}",
            )
        existing = next((item for item in platforms if item.get("title") == wanted), None)
        if existing is not None:
            return platforms, None
        if self.config.dry_run:
            return platforms, SyncPlan(
                "platform_required",
                state.ra_game_id,
                state.title,
                f"would register Backloggery platform {wanted}",
            )

        refreshed = self.backloggery.platforms()
        existing = next((item for item in refreshed if item.get("title") == wanted), None)
        if existing is not None:
            return refreshed, None
        matches = [item for item in self.backloggery.platform_catalog() if item.get("title") == wanted]
        if len(matches) != 1:
            raise UpstreamError(f"Backloggery platform catalog did not contain one exact {wanted} match")
        catalog_platform = matches[0]
        self.backloggery.add_platform(catalog_platform)
        confirmed = self.backloggery.platforms()
        registered = next(
            (
                item
                for item in confirmed
                if item.get("title") == wanted
                and int(item.get("platform_id", 0)) == int(catalog_platform["platform_id"])
            ),
            None,
        )
        if registered is None:
            raise UpstreamError(f"Backloggery did not confirm the new {wanted} platform")
        logger.info(
            {
                "event": "platform_registered",
                "platform": wanted,
                "platform_id": int(registered["platform_id"]),
            }
        )
        return confirmed, None

    def cycle(self) -> CycleResult:
        recent = self.ra.recently_played()
        if recent is None:
            plan = SyncPlan("noop", 0, "", "no recently played game")
            self._sync_priority_lifecycle(None)
            self.store.heartbeat(True)
            return CycleResult(plan, False, 0)
        game_id = int(recent.get("GameID") or recent.get("game_id") or recent.get("ID") or 0)
        summary = self.ra.summary(game_id)
        progress = self.ra.game_progress(game_id)
        hashes = self.ra.game_hashes(game_id)
        extended = self.ra.game_extended(game_id)
        state = normalize_game_state(recent, summary, progress, hashes, extended)
        if state.title_resolution_warning:
            logger.warning({
                "event": "title_resolution",
                "ra_game_id": state.ra_game_id,
                "title": state.title,
                "reason": state.title_resolution_warning,
            })
        logger.info(
            {
                "event": "rich_presence",
                "title": state.title,
                "value": state.notes,
            }
        )
        if state.region is None:
            plan = SyncPlan(
                "blocked",
                state.ra_game_id,
                state.title,
                "region could not be derived from supported hashes",
            )
            logger.info(
                {
                    "event": "sync_plan",
                    "dry_run": self.config.dry_run,
                    "plan": plan.as_dict(),
                    "summary": plan.reason,
                }
            )
            watch = self.store.observe(
                state.ra_game_id,
                state.fingerprint,
                state.online,
                self.config.offline_stable_polls,
            )
            self._sync_priority_lifecycle(state, watch.active)
            self.store.heartbeat(True)
            return CycleResult(plan, watch.active, watch.stable_polls)
        platforms = self.backloggery.platforms()
        platforms, platform_plan = self._ensure_platform(state, platforms)
        if platform_plan is not None:
            logger.info(
                {
                    "event": "sync_plan",
                    "dry_run": self.config.dry_run,
                    "plan": platform_plan.as_dict(),
                    "summary": platform_plan.reason,
                }
            )
            watch = self.store.observe(
                state.ra_game_id,
                state.fingerprint,
                state.online,
                self.config.offline_stable_polls,
            )
            self._sync_priority_lifecycle(state, watch.active)
            self.store.heartbeat(True)
            return CycleResult(platform_plan, watch.active, watch.stable_polls)
        library = self.backloggery.library(self.config.backloggery_username)
        mapping = self.store.get_mapping(state.ra_game_id)
        existing = self.backloggery.game(mapping) if mapping is not None else None
        field_overrides = None
        if mapping is not None and existing is not None and state.region is not None:
            field_overrides = self.store.reconcile_field_overrides(
                state.ra_game_id,
                {"title": existing.get("title"), "region": existing.get("region")},
                {"title": state.title, "region": REGION_CODES[state.region]},
            )
        plan = build_plan(state, platforms, library, mapping, existing, field_overrides)
        logger.info(
            {
                "event": "sync_plan",
                "dry_run": self.config.dry_run,
                "plan": plan.as_dict(),
                "summary": self._summary(plan),
            }
        )
        if not self.config.dry_run:
            plan = self._apply_live_plan(state, platforms, plan, field_overrides)
        watch = self.store.observe(
            state.ra_game_id,
            state.fingerprint,
            state.online,
            self.config.offline_stable_polls,
        )
        self._sync_priority_lifecycle(state, watch.active)
        self.store.heartbeat(True)
        return CycleResult(plan, watch.active, watch.stable_polls)

    def _steam_scan(self) -> None:
        """Refresh playtime, achievement counts, Notes, and status for linked PC entries."""
        if self.steam is None:
            return
        prior = {
            int(item["steam_appid"]): item
            for item in self.store.list_steam_candidates(limit=100000)
        }
        games = self.steam.owned_games()
        recent_games = self.steam.recently_played()
        self.store.save_steam_candidates(games, recent_games)
        changed_entries = 0
        for candidate in self.store.list_steam_candidates(limit=100000):
            app_id = int(candidate["steam_appid"])
            entry_id = candidate.get("backloggery_game_inst_id")
            if entry_id is None:
                continue
            previous = prior.get(app_id)
            recent_minutes = int(candidate.get("playtime_recent") or 0)
            lifetime_minutes = int(candidate.get("playtime_forever") or 0)
            playtime_changed = previous is None or any(
                int(previous.get(field) or 0) != current
                for field, current in (
                    ("playtime_forever", lifetime_minutes),
                    ("playtime_recent", recent_minutes),
                )
            )
            earned = candidate.get("achievements_earned")
            total = candidate.get("achievements_total")
            achievement_status = str(candidate.get("achievement_status") or "unavailable")
            raw_achievements = candidate.get("achievements_json")
            try:
                achievements = json.loads(str(raw_achievements)) if raw_achievements else []
            except (TypeError, ValueError):
                achievements = []
            if not isinstance(achievements, list) or not all(
                isinstance(name, str) for name in achievements
            ):
                achievements = []
            cached_achievements = list(achievements)
            if playtime_changed or recent_minutes > 0 or earned is None or total is None:
                try:
                    stats = self.steam.player_achievements(app_id)
                except UpstreamError:
                    pass
                else:
                    achievements = latest_unlocked_achievements(stats)
                    earned, total = achievement_counts(stats)
                    achievement_status = "available"
            notes = format_steam_notes(
                recent_minutes,
                lifetime_minutes,
                achievements if achievement_status == "available" else None,
            )
            if (
                notes != candidate.get("notes")
                or achievements != cached_achievements
                or earned != candidate.get("achievements_earned")
                or total != candidate.get("achievements_total")
                or achievement_status != candidate.get("achievement_status")
            ):
                self.store.set_steam_candidate_details(
                    app_id, notes, achievements, achievement_status, earned, total
                )
            mastered = earned is not None and total is not None and int(total) > 0 and int(earned) >= int(total)
            tracking = self.store.steam_priority_tracking(app_id)
            if tracking is not None:
                # Recent playtime is a rolling two-week total. Keep any actual
                # session lifecycle untouched and only refresh mastery metadata.
                self.store.set_steam_priority_tracking(
                    app_id,
                    bool(tracking["recently_played"]),
                    str(tracking["inactive_since"]) if tracking.get("inactive_since") else None,
                    mastered,
                    active_session=bool(tracking.get("active_session")),
                    missed_presence_polls=int(tracking.get("missed_presence_polls") or 0),
                )
            desired_status: int | None = None
            if mastered:
                desired_status = 40
            elif candidate.get("playtime_forever") is not None:
                desired_status = 20 if int(candidate.get("playtime_forever") or 0) > 0 else 10
            try:
                latest = self.backloggery.game(int(entry_id))
            except UpstreamError as exc:
                logger.warning({
                    "event": "steam_sync_skipped",
                    "appid": app_id,
                    "reason": f"Backloggery read failed: {exc}",
                })
                continue
            if latest is None:
                logger.warning({"event": "steam_sync_skipped", "appid": app_id, "reason": "linked entry not found"})
                continue
            payload = dict(latest)
            payload["game_inst_id"] = int(entry_id)
            changed = False
            changed_fields: list[str] = []
            if desired_status is not None:
                current_status = int(latest.get("status", 0) or 0)
                status = 40 if mastered else max(current_status, desired_status)
                if status != current_status:
                    payload["status"] = status
                    changed = True
                    changed_fields.append("status")
            if str(latest.get("notes") or "") != notes:
                payload["notes"] = notes
                changed = True
                changed_fields.append("notes")
            if earned is not None and total is not None:
                for field, desired in (("achieve_score", int(earned)), ("achieve_total", int(total))):
                    try:
                        current = int(latest.get(field))
                    except (TypeError, ValueError):
                        current = None
                    if current != desired:
                        payload[field] = desired
                        changed = True
                        changed_fields.append(field)
            if changed:
                if not self.config.dry_run:
                    payload["prev_status"] = latest.get("status")
                    payload["prev_own"] = latest.get("own")
                    try:
                        self.backloggery.update_game(payload)
                        confirmed = self.backloggery.game(int(entry_id))
                        if confirmed is None:
                            raise UpstreamError("Backloggery Steam entry update could not be verified")
                        if str(confirmed.get("notes") or "") != notes:
                            raise UpstreamError("Backloggery Steam Notes update could not be verified")
                        if earned is not None and total is not None:
                            try:
                                confirmed_earned = int(confirmed.get("achieve_score"))
                                confirmed_total = int(confirmed.get("achieve_total"))
                            except (TypeError, ValueError) as exc:
                                raise UpstreamError(
                                    "Backloggery Steam achievement counts could not be verified"
                                ) from exc
                            if confirmed_earned != int(earned) or confirmed_total != int(total):
                                raise UpstreamError(
                                    "Backloggery Steam achievement counts did not reach the requested values"
                                )
                    except UpstreamError as exc:
                        logger.warning({
                            "event": "steam_sync_skipped",
                            "appid": app_id,
                            "reason": f"Backloggery update could not be confirmed: {exc}",
                        })
                        continue
                changed_entries += 1
                logger.info({
                    "event": "steam_entry_updated" if not self.config.dry_run else "steam_entry_update_planned",
                    "appid": app_id,
                    "backloggery_entry_id": int(entry_id),
                    "changed_fields": changed_fields,
                    "dry_run": self.config.dry_run,
                })
        logger.info({
            "event": "steam_scan_complete",
            "owned": len(games),
            "recently_played": len(recent_games),
            "linked_entries_changed": changed_entries,
        })

    def _steam_presence_poll(self) -> None:
        """Use Steam's exact active AppID for Now Playing and start decay on session end."""
        if self.steam is None:
            return
        active_app_id = self.steam.currently_playing()
        if active_app_id is not None and self.config.steam_auto_submit_active:
            self._steam_auto_submit_active_game(active_app_id)
        now = datetime.now(UTC)
        candidates = {
            int(item["steam_appid"]): item
            for item in self.store.list_steam_candidates(limit=100000)
            if item.get("backloggery_game_inst_id") is not None
        }
        tracking_ids = {
            int(row["steam_appid"])
            for app_id in candidates
            if (row := self.store.steam_priority_tracking(app_id)) is not None
        }
        if active_app_id in candidates:
            tracking_ids.add(active_app_id)

        interval = timedelta(days=self.config.priority_decay_days)
        for app_id in sorted(tracking_ids):
            candidate = candidates[app_id]
            entry_id = int(candidate["backloggery_game_inst_id"])
            tracking = self.store.steam_priority_tracking(app_id)
            candidate_earned = candidate.get("achievements_earned")
            candidate_total = candidate.get("achievements_total")
            mastered = (
                int(candidate_total) > 0 and int(candidate_earned) >= int(candidate_total)
                if candidate_earned is not None and candidate_total is not None
                else bool(tracking["mastered"]) if tracking else False
            )
            is_active = app_id == active_app_id
            missed = int(tracking.get("missed_presence_polls") or 0) if tracking else 0
            inactive_since = tracking.get("inactive_since") if tracking else None

            if is_active:
                confirmed_active = True
                missed = 0
                inactive_since = None
            elif tracking and bool(tracking.get("active_session")):
                missed += 1
                confirmed_active = missed < 2
                if confirmed_active:
                    inactive_since = None
                else:
                    inactive_since = now.isoformat()
            else:
                confirmed_active = False
                # Migrate legacy tracking from rolling recent playtime: decay
                # begins now rather than treating that window as live presence.
                if tracking and bool(tracking.get("recently_played")) and not inactive_since:
                    inactive_since = now.isoformat()
                missed = 0

            self.store.set_steam_priority_tracking(
                app_id,
                False,
                str(inactive_since) if inactive_since else None,
                mastered,
                active_session=confirmed_active,
                missed_presence_polls=missed,
            )

            desired_priority: int | None = PRIORITY_NOW_PLAYING if confirmed_active else None
            reason = "Steam player summary reports this AppID as active"
            if not confirmed_active and inactive_since:
                elapsed = now - datetime.fromisoformat(str(inactive_since)).astimezone(UTC)
                if elapsed >= interval * 2 and mastered:
                    desired_priority = PRIORITY_NORMAL
                    reason = "mastered game returns to Normal after two inactive intervals"
                elif elapsed >= interval * 3:
                    desired_priority = PRIORITY_LOW
                    reason = "Steam session ended three priority intervals ago"
                elif elapsed >= interval * 2:
                    desired_priority = PRIORITY_HIGH
                    reason = "Steam session ended two priority intervals ago"
                elif elapsed >= interval:
                    desired_priority = PRIORITY_PAUSED
                    reason = "Steam session ended one priority interval ago"
            if desired_priority is None:
                continue

            latest = self.backloggery.game(entry_id)
            if latest is None:
                logger.warning({"event": "steam_presence_skipped", "appid": app_id,
                                "reason": "linked Backloggery entry not found"})
                continue
            try:
                current_priority = int(latest.get("priority", PRIORITY_NORMAL))
            except (TypeError, ValueError):
                current_priority = PRIORITY_NORMAL
            if current_priority == desired_priority:
                continue
            if not confirmed_active and current_priority not in {
                PRIORITY_NOW_PLAYING, PRIORITY_PAUSED, PRIORITY_HIGH,
                PRIORITY_NORMAL, PRIORITY_LOW,
            }:
                # Preserve manually selected Ongoing, Replay, and Shelved priorities.
                continue
            event = {
                "event": "steam_priority_plan",
                "dry_run": self.config.dry_run,
                "appid": app_id,
                "backloggery_entry_id": entry_id,
                "title": latest.get("title"),
                "from": PRIORITY_NAMES.get(current_priority, str(current_priority)),
                "to": PRIORITY_NAMES[desired_priority],
                "reason": reason,
            }
            if self.config.dry_run:
                logger.info(event)
                continue
            payload = dict(latest)
            payload["priority"] = desired_priority
            payload["prev_status"] = latest.get("status")
            payload["prev_own"] = latest.get("own")
            self.backloggery.update_game(payload)
            confirmed = self.backloggery.game(entry_id)
            try:
                confirmed_priority = int(confirmed.get("priority", -1)) if confirmed else -1
            except (TypeError, ValueError):
                confirmed_priority = -1
            if confirmed_priority != desired_priority:
                raise UpstreamError("Backloggery Steam priority update could not be verified")
            event["event"] = "steam_priority_updated"
            logger.info(event)

    def _steam_auto_submit_active_game(self, app_id: int) -> None:
        """Submit an active, officially owned Steam game unless review excluded it."""
        if self.steam is None:
            return
        candidate = self.store.get_steam_candidate(app_id)
        if candidate is not None:
            if candidate.get("backloggery_game_inst_id") is not None:
                return
            if candidate.get("review_status") in {
                "discarded", "ignored", "skipped", "already_tracked",
            }:
                return

        games = self.steam.owned_games()
        if not any(int(game["appid"]) == app_id for game in games):
            logger.debug({"event": "steam_active_game_not_owned", "appid": app_id})
            return
        recent_games = self.steam.recently_played()
        self.store.save_steam_candidates(games, recent_games)
        candidate = self.store.get_steam_candidate(app_id)
        if candidate is None:
            return
        if self.config.dry_run:
            logger.info({
                "event": "steam_active_game_submission_planned",
                "appid": app_id,
                "dry_run": True,
            })
            return
        if candidate["review_status"] == "unreviewed":
            self.store.review_steam_candidate(app_id, "accepted")
        result = submit_steam_candidate(
            self.store,
            self.backloggery,
            self.steam,
            app_id,
            self.config.backloggery_username,
        )
        logger.info({
            "event": "steam_active_game_submitted",
            "appid": app_id,
            "action": result["action"],
            "backloggery_entry_id": result["game_inst_id"],
        })

    def run(self) -> None:
        logger.info(
            {
                "event": "service_started",
                "dry_run": self.config.dry_run,
                "offline_poll_seconds": self.config.offline_poll_seconds,
                "online_poll_seconds": self.config.online_poll_seconds,
                "priority_decay_days": self.config.priority_decay_days,
                "steam_scan_seconds": self.config.steam_scan_seconds if self.steam else None,
                "steam_presence_seconds": self.config.steam_presence_seconds if self.steam else None,
                "steam_auto_submit_active": self.config.steam_auto_submit_active if self.steam else False,
            }
        )
        last_steam_scan = 0.0
        last_steam_presence = 0.0
        while True:
            try:
                result = self.cycle()
                delay = self.config.online_poll_seconds if result.active_watch else self.config.offline_poll_seconds
            except AuthenticationError as exc:
                self.store.heartbeat(False, "authentication")
                logger.error({"event": "cycle_degraded", "code": "authentication", "message": str(exc)})
                delay = self.config.offline_poll_seconds
            except UpstreamError as exc:
                self.store.heartbeat(False, "upstream")
                logger.error({"event": "cycle_degraded", "code": "upstream", "message": str(exc)})
                delay = self.config.offline_poll_seconds
            if (
                self.steam is not None
                and time.monotonic() - last_steam_presence >= self.config.steam_presence_seconds
            ):
                try:
                    self._steam_presence_poll()
                except AuthenticationError as exc:
                    logger.error({"event": "steam_presence_degraded", "code": "authentication", "message": str(exc)})
                except UpstreamError as exc:
                    logger.error({"event": "steam_presence_degraded", "code": "upstream", "message": str(exc)})
                last_steam_presence = time.monotonic()
            if self.steam is not None and time.monotonic() - last_steam_scan >= self.config.steam_scan_seconds:
                try:
                    self._steam_scan()
                except AuthenticationError as exc:
                    logger.error({"event": "steam_scan_degraded", "code": "authentication", "message": str(exc)})
                except UpstreamError as exc:
                    logger.error({"event": "steam_scan_degraded", "code": "upstream", "message": str(exc)})
                last_steam_scan = time.monotonic()
            if self.steam is not None:
                until_presence = max(
                    1,
                    self.config.steam_presence_seconds - (time.monotonic() - last_steam_presence),
                )
                until_scan = max(
                    1,
                    self.config.steam_scan_seconds - (time.monotonic() - last_steam_scan),
                )
                delay = min(delay, until_presence, until_scan)
            time.sleep(delay)
