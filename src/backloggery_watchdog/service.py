from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .backloggery import BackloggeryClient
from .config import Config
from .errors import AuthenticationError, UpstreamError
from .models import GameState, SyncPlan
from .planner import REGION_CODES, STATUS_NAMES, build_plan, resolve_platform_title
from .ra import RetroAchievementsClient, normalize_game_state
from .state import StateStore

logger = logging.getLogger(__name__)


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
    ):
        self.config = config
        self.store = store
        self.ra = ra
        self.backloggery = backloggery

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
            entry_id = self.backloggery.add_game(refreshed.proposed_payload)
            self.store.set_mapping(state.ra_game_id, entry_id)
            confirmed = self.backloggery.game(entry_id)
            if confirmed is None:
                raise UpstreamError("Backloggery created an entry but it could not be verified")
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
            refreshed = build_plan(
                state,
                platforms,
                self.backloggery.library(self.config.backloggery_username),
                plan.backloggery_entry_id,
                latest,
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
            )
            if verification.action == "update":
                raise UpstreamError("Backloggery update did not reach the requested managed state")
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
            self.store.heartbeat(True)
            return CycleResult(plan, False, 0)
        game_id = int(recent.get("GameID") or recent.get("game_id") or recent.get("ID") or 0)
        summary = self.ra.summary(game_id)
        progress = self.ra.game_progress(game_id)
        hashes = self.ra.game_hashes(game_id)
        state = normalize_game_state(recent, summary, progress, hashes)
        logger.info(
            {
                "event": "rich_presence",
                "title": state.title,
                "value": state.rich_presence,
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
            self.store.heartbeat(True)
            return CycleResult(platform_plan, watch.active, watch.stable_polls)
        library = self.backloggery.library(self.config.backloggery_username)
        mapping = self.store.get_mapping(state.ra_game_id)
        existing = self.backloggery.game(mapping) if mapping is not None else None
        plan = build_plan(state, platforms, library, mapping, existing)
        logger.info(
            {
                "event": "sync_plan",
                "dry_run": self.config.dry_run,
                "plan": plan.as_dict(),
                "summary": self._summary(plan),
            }
        )
        if not self.config.dry_run:
            plan = self._apply_live_plan(state, platforms, plan)
        watch = self.store.observe(
            state.ra_game_id,
            state.fingerprint,
            state.online,
            self.config.offline_stable_polls,
        )
        self.store.heartbeat(True)
        return CycleResult(plan, watch.active, watch.stable_polls)

    def run(self) -> None:
        logger.info(
            {
                "event": "service_started",
                "dry_run": self.config.dry_run,
                "offline_poll_seconds": self.config.offline_poll_seconds,
                "online_poll_seconds": self.config.online_poll_seconds,
            }
        )
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
            time.sleep(delay)
