from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .backloggery import BackloggeryClient
from .config import Config
from .errors import AuthenticationError, UpstreamError
from .models import GameState, SyncPlan
from .planner import REGION_CODES, STATUS_NAMES, build_plan
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
        platforms = self.backloggery.platforms()
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
