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
            CREATE TABLE IF NOT EXISTS priority_tracking (
                ra_game_id INTEGER PRIMARY KEY,
                last_fingerprint TEXT NOT NULL,
                offline_since TEXT,
                online INTEGER NOT NULL,
                mastered INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS steam_candidates (
                steam_appid INTEGER PRIMARY KEY,
                steam_name TEXT NOT NULL,
                playtime_forever INTEGER NOT NULL DEFAULT 0,
                playtime_recent INTEGER NOT NULL DEFAULT 0,
                owned INTEGER NOT NULL DEFAULT 1,
                access_source TEXT NOT NULL DEFAULT 'owned_api',
                first_seen_at TEXT NOT NULL,
                last_seen_in_sync TEXT NOT NULL,
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                backloggery_game_inst_id INTEGER,
                submitted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS steam_candidate_details (
                steam_appid INTEGER PRIMARY KEY,
                notes TEXT NOT NULL,
                achievements_json TEXT NOT NULL,
                achievement_status TEXT NOT NULL,
                achievements_earned INTEGER,
                achievements_total INTEGER,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS steam_priority_tracking (
                steam_appid INTEGER PRIMARY KEY,
                recently_played INTEGER NOT NULL,
                inactive_since TEXT,
                mastered INTEGER NOT NULL,
                active_session INTEGER NOT NULL DEFAULT 0,
                missed_presence_polls INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        existing_candidate_columns = {
            str(row[1]) for row in self.db.execute("PRAGMA table_info(steam_candidates)")
        }
        for column, definition in (
            ("canonical_title", "TEXT"),
            ("platform", "TEXT"),
            ("subsystem", "TEXT"),
            ("review_notes", "TEXT"),
            ("backloggery_game_inst_id", "INTEGER"),
            ("submitted_at", "TEXT"),
            ("access_source", "TEXT NOT NULL DEFAULT 'owned_api'"),
        ):
            if column not in existing_candidate_columns:
                self.db.execute(f"ALTER TABLE steam_candidates ADD COLUMN {column} {definition}")
        existing_detail_columns = {
            str(row[1]) for row in self.db.execute("PRAGMA table_info(steam_candidate_details)")
        }
        for column in ("achievements_earned", "achievements_total"):
            if column not in existing_detail_columns:
                self.db.execute(
                    f"ALTER TABLE steam_candidate_details ADD COLUMN {column} INTEGER"
                )
        existing_priority_columns = {
            str(row[1]) for row in self.db.execute("PRAGMA table_info(steam_priority_tracking)")
        }
        for column, definition in (
            ("active_session", "INTEGER NOT NULL DEFAULT 0"),
            ("missed_presence_polls", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in existing_priority_columns:
                self.db.execute(
                    f"ALTER TABLE steam_priority_tracking ADD COLUMN {column} {definition}"
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

    def priority_tracking(self) -> list[dict[str, object]]:
        rows = self.db.execute(
            "SELECT ra_game_id, last_fingerprint, offline_since, online, mastered "
            "FROM priority_tracking ORDER BY ra_game_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_priority_tracking(self, ra_game_id: int) -> dict[str, object] | None:
        row = self.db.execute(
            "SELECT ra_game_id, last_fingerprint, offline_since, online, mastered "
            "FROM priority_tracking WHERE ra_game_id=?",
            (ra_game_id,),
        ).fetchone()
        return dict(row) if row else None

    def set_priority_tracking(
        self,
        ra_game_id: int,
        fingerprint: str,
        offline_since: str | None,
        online: bool,
        mastered: bool,
    ) -> None:
        self.db.execute(
            """INSERT INTO priority_tracking
               (ra_game_id, last_fingerprint, offline_since, online, mastered, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(ra_game_id) DO UPDATE SET
                 last_fingerprint=excluded.last_fingerprint,
                 offline_since=excluded.offline_since,
                 online=excluded.online,
                 mastered=excluded.mastered,
                 updated_at=excluded.updated_at""",
            (ra_game_id, fingerprint, offline_since, int(online), int(mastered), utc_now()),
        )
        self.db.commit()

    def steam_priority_tracking(self, app_id: int) -> dict[str, object] | None:
        row = self.db.execute(
            "SELECT steam_appid, recently_played, inactive_since, mastered, active_session, "
            "missed_presence_polls, updated_at "
            "FROM steam_priority_tracking WHERE steam_appid=?", (int(app_id),)
        ).fetchone()
        return dict(row) if row else None

    def set_steam_priority_tracking(
        self, app_id: int, recently_played: bool, inactive_since: str | None,
        mastered: bool, *, active_session: bool = False, missed_presence_polls: int = 0,
    ) -> None:
        self.db.execute(
            """INSERT INTO steam_priority_tracking
               (steam_appid, recently_played, inactive_since, mastered, active_session,
                missed_presence_polls, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(steam_appid) DO UPDATE SET
                 recently_played=excluded.recently_played,
                 inactive_since=excluded.inactive_since,
                 mastered=excluded.mastered,
                 active_session=excluded.active_session,
                 missed_presence_polls=excluded.missed_presence_polls,
                 updated_at=excluded.updated_at""",
            (int(app_id), int(recently_played), inactive_since, int(mastered),
             int(active_session), int(missed_presence_polls), utc_now()),
        )
        self.db.commit()

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

    def save_steam_candidates(
        self, games: list[dict[str, object]], recently_played: list[dict[str, object]]
    ) -> dict[str, int]:
        now = utc_now()
        recent_minutes = {
            int(item["appid"]): int(item.get("playtime_2weeks", 0) or 0)
            for item in recently_played
        }
        new_count = changed_count = 0
        for game in games:
            appid = int(game["appid"])
            name = str(game.get("name") or f"Steam App {appid}")
            lifetime = int(game.get("playtime_forever", 0) or 0)
            recent = recent_minutes.get(appid, int(game.get("playtime_2weeks", 0) or 0))
            previous = self.db.execute(
                """SELECT steam_name, playtime_forever, playtime_recent
                   FROM steam_candidates WHERE steam_appid=?""",
                (appid,),
            ).fetchone()
            if previous is None:
                new_count += 1
            elif (
                str(previous["steam_name"]),
                int(previous["playtime_forever"]),
                int(previous["playtime_recent"]),
            ) != (name, lifetime, recent):
                changed_count += 1
            self.db.execute(
                """INSERT INTO steam_candidates
                   (steam_appid, steam_name, playtime_forever, playtime_recent, owned, access_source, first_seen_at, last_seen_in_sync)
                   VALUES (?, ?, ?, ?, 1, 'owned_api', ?, ?)
                   ON CONFLICT(steam_appid) DO UPDATE SET
                     steam_name=excluded.steam_name,
                     playtime_forever=excluded.playtime_forever,
                     playtime_recent=excluded.playtime_recent,
                     owned=1,
                     access_source='owned_api',
                     last_seen_in_sync=excluded.last_seen_in_sync""",
                (appid, name, lifetime, recent, now, now),
            )
        self.db.commit()
        return {"new": new_count, "changed": changed_count, "seen": len(games)}

    def save_steam_inferred_family_candidate(
        self, app_id: int, name: str, recently_played: list[dict[str, object]]
    ) -> None:
        """Save a catalog-verified active game without claiming license ownership."""
        recent = next(
            (item for item in recently_played if int(item["appid"]) == app_id), None
        )
        minutes_recent = int(recent.get("playtime_2weeks", 0) or 0) if recent else 0
        minutes_lifetime = int(recent.get("playtime_forever", 0) or 0) if recent else 0
        now = utc_now()
        self.db.execute(
            """INSERT INTO steam_candidates
               (steam_appid, steam_name, playtime_forever, playtime_recent, owned,
                access_source, first_seen_at, last_seen_in_sync)
               VALUES (?, ?, ?, ?, 0, 'family_inferred', ?, ?)
               ON CONFLICT(steam_appid) DO UPDATE SET
                 playtime_forever=MAX(steam_candidates.playtime_forever, excluded.playtime_forever),
                 playtime_recent=excluded.playtime_recent,
                 last_seen_in_sync=excluded.last_seen_in_sync""",
            (app_id, name, minutes_lifetime, minutes_recent, now, now),
        )
        self.db.commit()

    def refresh_steam_inferred_family_playtime(
        self, recently_played: list[dict[str, object]]
    ) -> None:
        recent = {int(item["appid"]): item for item in recently_played}
        for row in self.db.execute(
            "SELECT steam_appid, playtime_forever FROM steam_candidates WHERE access_source='family_inferred'"
        ).fetchall():
            app_id = int(row["steam_appid"])
            item = recent.get(app_id)
            lifetime = max(
                int(row["playtime_forever"]),
                int(item.get("playtime_forever", 0) or 0) if item else 0,
            )
            minutes_recent = int(item.get("playtime_2weeks", 0) or 0) if item else 0
            self.db.execute(
                "UPDATE steam_candidates SET playtime_forever=?, playtime_recent=? WHERE steam_appid=?",
                (lifetime, minutes_recent, app_id),
            )
        self.db.commit()

    def list_steam_candidates(
        self, limit: int = 20, status: str | None = None
    ) -> list[dict[str, object]]:
        query = """SELECT c.steam_appid, c.steam_name, c.playtime_forever, c.playtime_recent,
                          c.owned, c.access_source, c.first_seen_at, c.last_seen_in_sync, c.review_status,
                          c.canonical_title, c.platform, c.subsystem, c.review_notes,
                          c.backloggery_game_inst_id, c.submitted_at,
                          d.notes, d.achievements_json, d.achievement_status,
                          d.achievements_earned, d.achievements_total
                   FROM steam_candidates c LEFT JOIN steam_candidate_details d
                     ON d.steam_appid=c.steam_appid"""
        params: list[object] = []
        if status is not None:
            query += " WHERE c.review_status=?"
            params.append(status)
        query += " ORDER BY c.playtime_recent DESC, c.playtime_forever DESC, c.steam_name COLLATE NOCASE LIMIT ?"
        params.append(limit)
        rows = self.db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_steam_candidate(self, app_id: int) -> dict[str, object] | None:
        row = self.db.execute(
            """SELECT c.steam_appid, c.steam_name, c.playtime_forever, c.playtime_recent,
                      c.owned, c.access_source, c.first_seen_at, c.last_seen_in_sync, c.review_status,
                      c.canonical_title, c.platform, c.subsystem, c.review_notes,
                      c.backloggery_game_inst_id, c.submitted_at,
                      d.notes, d.achievements_json, d.achievement_status,
                      d.achievements_earned, d.achievements_total
               FROM steam_candidates c LEFT JOIN steam_candidate_details d
                 ON d.steam_appid=c.steam_appid WHERE c.steam_appid=?""",
            (int(app_id),),
        ).fetchone()
        return dict(row) if row else None

    def set_steam_candidate_details(
        self,
        app_id: int,
        notes: str,
        achievements: list[str],
        achievement_status: str,
        achievements_earned: int | None = None,
        achievements_total: int | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO steam_candidate_details
               (steam_appid, notes, achievements_json, achievement_status,
                achievements_earned, achievements_total, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(steam_appid) DO UPDATE SET
                 notes=excluded.notes,
                 achievements_json=excluded.achievements_json,
                 achievement_status=excluded.achievement_status,
                 achievements_earned=excluded.achievements_earned,
                 achievements_total=excluded.achievements_total,
                 updated_at=excluded.updated_at""",
            (
                int(app_id),
                notes,
                self._dump_value(achievements),
                achievement_status,
                achievements_earned,
                achievements_total,
                utc_now(),
            ),
        )
        self.db.commit()

    def set_steam_candidate_backloggery_link(self, app_id: int, entry_id: int) -> None:
        self.db.execute(
            """UPDATE steam_candidates
               SET backloggery_game_inst_id=?, submitted_at=? WHERE steam_appid=?""",
            (int(entry_id), utc_now(), int(app_id)),
        )
        if self.db.execute("SELECT changes()").fetchone()[0] != 1:
            raise ValueError(f"unknown Steam AppID {app_id}")
        self.db.commit()

    def review_steam_candidate(
        self,
        app_id: int,
        status: str,
        *,
        canonical_title: str | None = None,
        platform: str | None = None,
        subsystem: str | None = None,
        review_notes: str | None = None,
    ) -> None:
        allowed = {
            "accepted",
            "accepted_with_edits",
            "discarded",
            "skipped",
            "ignored",
            "already_tracked",
        }
        if status not in allowed:
            raise ValueError("unsupported Steam candidate review status")
        if self.get_steam_candidate(app_id) is None:
            raise ValueError(f"unknown Steam AppID {app_id}")
        if status == "accepted_with_edits" and all(
            value is None for value in (canonical_title, platform, subsystem, review_notes)
        ):
            raise ValueError("edit review requires at least one edited field")
        self.db.execute(
            """UPDATE steam_candidates SET
                 review_status=?,
                 canonical_title=COALESCE(?, canonical_title),
                 platform=COALESCE(?, platform),
                 subsystem=COALESCE(?, subsystem),
                 review_notes=COALESCE(?, review_notes)
               WHERE steam_appid=?""",
            (status, canonical_title, platform, subsystem, review_notes, int(app_id)),
        )
        self.db.commit()
