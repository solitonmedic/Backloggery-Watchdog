from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class WatchState:
    active: bool
    stable_polls: int


class StateStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS mappings (
                ra_game_id INTEGER PRIMARY KEY,
                backloggery_game_inst_id INTEGER NOT NULL UNIQUE,
                confirmed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observations (
                ra_game_id INTEGER PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                active INTEGER NOT NULL,
                stable_polls INTEGER NOT NULL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def set_mapping(self, ra_game_id: int, backloggery_game_inst_id: int) -> None:
        self.db.execute(
            """INSERT INTO mappings (ra_game_id, backloggery_game_inst_id, confirmed_at)
               VALUES (?, ?, ?)
               ON CONFLICT(ra_game_id) DO UPDATE SET
                 backloggery_game_inst_id=excluded.backloggery_game_inst_id,
                 confirmed_at=excluded.confirmed_at""",
            (ra_game_id, backloggery_game_inst_id, utc_now()),
        )
        self.db.commit()

    def get_mapping(self, ra_game_id: int) -> int | None:
        row = self.db.execute(
            "SELECT backloggery_game_inst_id FROM mappings WHERE ra_game_id=?", (ra_game_id,)
        ).fetchone()
        return int(row[0]) if row else None

    def list_mappings(self) -> list[dict[str, object]]:
        rows = self.db.execute(
            "SELECT ra_game_id, backloggery_game_inst_id, confirmed_at FROM mappings ORDER BY ra_game_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def observe(self, ra_game_id: int, fingerprint: str, online: bool, stable_limit: int) -> WatchState:
        prior = self.db.execute(
            "SELECT fingerprint, active, stable_polls FROM observations WHERE ra_game_id=?",
            (ra_game_id,),
        ).fetchone()
        if online:
            active, stable = True, 0
        elif prior and bool(prior["active"]):
            stable = int(prior["stable_polls"]) + 1 if prior["fingerprint"] == fingerprint else 0
            active = stable < stable_limit
        else:
            active, stable = False, 0
        self.db.execute(
            """INSERT INTO observations (ra_game_id, fingerprint, active, stable_polls, observed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ra_game_id) DO UPDATE SET
                 fingerprint=excluded.fingerprint,
                 active=excluded.active,
                 stable_polls=excluded.stable_polls,
                 observed_at=excluded.observed_at""",
            (ra_game_id, fingerprint, int(active), stable, utc_now()),
        )
        self.db.commit()
        return WatchState(active=active, stable_polls=stable)

    def set_runtime(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO runtime (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

    def get_runtime(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def heartbeat(self, success: bool, error_code: str | None = None) -> None:
        now = utc_now()
        self.set_runtime("heartbeat_at", now)
        self.set_runtime("service_state", "ok" if success else "degraded")
        if success:
            self.set_runtime("last_success_at", now)
            self.set_runtime("last_error_code", "")
        elif error_code:
            self.set_runtime("last_error_code", error_code)
