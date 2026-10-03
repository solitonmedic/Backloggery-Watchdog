from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .backloggery import BackloggeryClient
from .config import Config
from .errors import AuthenticationError, UpstreamError
from .models import SyncPlan
from .planner import build_plan
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
        watch = self.store.observe(
            state.ra_game_id,
            state.fingerprint,
            state.online,
            self.config.offline_stable_polls,
        )
        self.store.heartbeat(True)
        logger.info(
            {
                "event": "sync_plan",
                "dry_run": True,
                "plan": plan.as_dict(),
                "active_watch": watch.active,
                "offline_stable_polls": watch.stable_polls,
            }
        )
        return CycleResult(plan, watch.active, watch.stable_polls)

    def run(self) -> None:
        logger.info({"event": "service_started", "dry_run": True})
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
