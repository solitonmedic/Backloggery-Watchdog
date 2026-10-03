from __future__ import annotations

import json
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
            CREATE TABLE IF NOT EXISTS field_sync_state (
                ra_game_id INTEGER NOT NULL,
                field_name TEXT NOT NULL CHECK (field_name IN ('title', 'region')),
                last_observed TEXT NOT NULL,
                override_value TEXT,
                PRIMARY KEY (ra_game_id, field_name)
            );
            """
        )
        self.db.commit()
        Path(path).chmod(0o600)

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
        result = []
        for row in rows:
            item = dict(row)
            item["overrides"] = self.get_field_overrides(int(item["ra_game_id"]))
            result.append(item)
        return result

    @staticmethod
    def _dump_value(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load_value(value: str | None) -> object | None:
        return json.loads(value) if value is not None else None

    def get_field_overrides(self, ra_game_id: int) -> dict[str, object]:
        rows = self.db.execute(
            "SELECT field_name, override_value FROM field_sync_state "
            "WHERE ra_game_id=? AND override_value IS NOT NULL",
            (ra_game_id,),
        ).fetchall()
        return {str(row["field_name"]): self._load_value(row["override_value"]) for row in rows}

    @staticmethod
    def _same(field_name: str, current: object, desired: object) -> bool:
        if field_name == "region":
            if current is None or desired is None:
                return current is desired
            try:
                return int(current) == int(desired)
            except (TypeError, ValueError):
                return False
        return current == desired

    def reconcile_field_overrides(
        self,
        ra_game_id: int,
        current_values: dict[str, object],
        desired_values: dict[str, object],
    ) -> dict[str, object]:
        """Persist user edits to title/region and return active overrides.

        On first observation, a value that differs from RA is conservatively
        treated as a user override so existing manual edits survive upgrade.
        Later changes are detected against the last value observed from the API.
        """
        for field_name in ("title", "region"):
            if field_name not in current_values or field_name not in desired_values:
                continue
            current = current_values[field_name]
            desired = desired_values[field_name]
            row = self.db.execute(
                "SELECT last_observed, override_value FROM field_sync_state "
                "WHERE ra_game_id=? AND field_name=?",
                (ra_game_id, field_name),
            ).fetchone()
            current_json = self._dump_value(current)
            override_json = None
            if row is None:
                if not self._same(field_name, current, desired):
                    override_json = current_json
            else:
                prior_value = self._load_value(row["last_observed"])
                prior_override = row["override_value"]
                if not self._same(field_name, current, prior_value):
                    if self._same(field_name, current, desired):
                        override_json = None
                    else:
                        override_json = current_json
                else:
                    override_json = prior_override
            self.db.execute(
                "INSERT INTO field_sync_state (ra_game_id, field_name, last_observed, override_value) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(ra_game_id, field_name) DO UPDATE SET "
                "last_observed=excluded.last_observed, override_value=excluded.override_value",
                (ra_game_id, field_name, current_json, override_json),
            )
        self.db.commit()
        return self.get_field_overrides(ra_game_id)

    def record_observed_fields(self, ra_game_id: int, values: dict[str, object]) -> None:
        for field_name in ("title", "region"):
            if field_name in values:
                self.db.execute(
                    "INSERT INTO field_sync_state "
                    "(ra_game_id, field_name, last_observed, override_value) VALUES (?, ?, ?, NULL) "
                    "ON CONFLICT(ra_game_id, field_name) DO UPDATE SET "
                    "last_observed=excluded.last_observed",
                    (ra_game_id, field_name, self._dump_value(values[field_name])),
                )
        self.db.commit()

    def clear_field_overrides(self, ra_game_id: int, field_name: str | None = None) -> None:
        if field_name is None:
            self.db.execute(
                "UPDATE field_sync_state SET override_value=NULL WHERE ra_game_id=?", (ra_game_id,)
            )
        else:
            if field_name not in {"title", "region"}:
                raise ValueError("field override must be title or region")
            self.db.execute(
                "UPDATE field_sync_state SET override_value=NULL WHERE ra_game_id=? AND field_name=?",
                (ra_game_id, field_name),
            )
        self.db.commit()

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
