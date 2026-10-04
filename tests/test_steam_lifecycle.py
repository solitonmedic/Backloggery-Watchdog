from __future__ import annotations

from dataclasses import replace

from backloggery_watchdog.service import WatchdogService
from backloggery_watchdog.state import StateStore

from test_service import config


class FakeRA:
    pass


class FakeSteam:
    def __init__(self, recent=25, active_app_id=None):
        self.recent = recent
        self.active_app_id = active_app_id
        self.earned = 2
        self.owned_calls = 0

    def currently_playing(self):
        return self.active_app_id

    def owned_games(self):
        self.owned_calls += 1
        return [{"appid": 42, "name": "Game", "playtime_forever": 500}]

    def recently_played(self):
        return [{"appid": 42, "playtime_2weeks": self.recent} ] if self.recent else []

    def player_achievements(self, _app_id):
        return [
            {"name": "Latest Achievement", "achieved": 1, "unlocktime": 100},
            {
                "name": "Other Achievement",
                "achieved": int(self.earned >= 2),
                "unlocktime": 50 if self.earned >= 2 else 0,
            },
        ]


class FakeBackloggery:
    def __init__(self):
        self.row = {
            "game_inst_id": 765,
            "title": "Game",
            "platform_id": 123,
            "status": 10,
            "priority": 40,
            "own": 1,
            "notes": "keep these notes",
        }
        self.updates = []
        self.added_games = []
        self.created_row = None
        self.library_rows = []

    def platforms(self):
        return [{"platform_id": 123, "title": "PC", "abbr": "PC"}]

    def library(self, _username):
        return list(self.library_rows)

    def add_game(self, payload):
        self.added_games.append(dict(payload))
        self.created_row = {"game_inst_id": 766, **payload}
        return 766

    def game(self, entry_id):
        if self.created_row is not None and entry_id == 766:
            return dict(self.created_row)
        for row in self.library_rows:
            if row["game_inst_id"] == entry_id:
                return dict(row)
        return dict(self.row)

    def update_game(self, payload):
        self.updates.append(dict(payload))
        if self.created_row is not None and payload["game_inst_id"] == 766:
            self.created_row.update(payload)
            return
        for row in self.library_rows:
            if row["game_inst_id"] == payload["game_inst_id"]:
                row.update(payload)
                return
        self.row.update(payload)


def _auto_setup(tmp_path, *, dry_run=False, active_app_id=42):
    store = StateStore(str(tmp_path / "steam.db"))
    backloggery = FakeBackloggery()
    steam = FakeSteam(active_app_id=active_app_id)
    service = WatchdogService(
        replace(config(tmp_path / "steam.db", dry_run=dry_run), steam_auto_submit_active=True),
        store, FakeRA(), backloggery, steam,
    )
    return store, backloggery, service


def _setup(tmp_path, *, recent=25, dry_run=False):
    store = StateStore(str(tmp_path / "steam.db"))
    store.save_steam_candidates(
        [{"appid": 42, "name": "Game", "playtime_forever": 500}],
        [{"appid": 42, "playtime_2weeks": recent}] if recent else [],
    )
    store.review_steam_candidate(42, "accepted")
    store.set_steam_candidate_backloggery_link(42, 765)
    store.set_steam_candidate_details(42, "notes", [], "available", 2, 2)
    backloggery = FakeBackloggery()
    service = WatchdogService(
        config(tmp_path / "steam.db", dry_run=dry_run),
        store,
        FakeRA(),
        backloggery,
        FakeSteam(recent),
    )
    return store, backloggery, service


def test_steam_scan_completes_mastered_game_without_claiming_live_presence(tmp_path):
    store, backloggery, service = _setup(tmp_path)
    service._steam_scan()
    assert backloggery.row["status"] == 40
    assert backloggery.row["priority"] == 40
    assert backloggery.row["notes"] == (
        "**Recent playtime:** *0 hours and 25 minutes*\n"
        "**Lifetime playtime:** *8 hours and 20 minutes*\n"
        "**Recent achievements:** *Latest Achievement*, *Other Achievement*"
    )
    assert backloggery.row["achieve_score"] == 2
    assert backloggery.row["achieve_total"] == 2
    assert len(backloggery.updates) == 1
    store.close()


def test_steam_scan_refreshes_notes_and_achievement_counts_when_playtime_changes(tmp_path):
    store, backloggery, service = _setup(tmp_path)
    service.steam.recent = 30
    service.steam.earned = 1

    service._steam_scan()

    assert backloggery.row["notes"] == (
        "**Recent playtime:** *0 hours and 30 minutes*\n"
        "**Lifetime playtime:** *8 hours and 20 minutes*\n"
        "**Recent achievements:** *Latest Achievement*"
    )
    assert backloggery.row["achieve_score"] == 1
    assert backloggery.row["achieve_total"] == 2
    assert len(backloggery.updates) == 1
    store.close()


def test_steam_scan_refreshes_achievements_for_games_with_recent_playtime(tmp_path):
    store, backloggery, service = _setup(tmp_path)
    service.steam.earned = 1

    service._steam_scan()

    assert backloggery.row["achieve_score"] == 1
    assert backloggery.row["achieve_total"] == 2
    assert backloggery.row["notes"] == (
        "**Recent playtime:** *0 hours and 25 minutes*\n"
        "**Lifetime playtime:** *8 hours and 20 minutes*\n"
        "**Recent achievements:** *Latest Achievement*"
    )
    store.close()


def test_steam_scan_skips_backloggery_write_when_managed_fields_match(tmp_path):
    store, backloggery, service = _setup(tmp_path)
    notes = (
        "**Recent playtime:** *0 hours and 25 minutes*\n"
        "**Lifetime playtime:** *8 hours and 20 minutes*\n"
        "**Recent achievements:** *Latest Achievement*, *Other Achievement*"
    )
    store.set_steam_candidate_details(42, notes, ["Latest Achievement", "Other Achievement"], "available", 2, 2)
    backloggery.row.update(
        {
            "status": 40,
            "notes": notes,
            "achieve_score": 2,
            "achieve_total": 2,
        }
    )

    service._steam_scan()

    assert backloggery.updates == []
    store.close()


def test_steam_scan_does_not_treat_rolling_recent_minutes_as_session_end(tmp_path):
    store, backloggery, service = _setup(tmp_path, recent=0)
    store.set_steam_priority_tracking(42, True, None, False)
    service._steam_scan()
    tracking = store.steam_priority_tracking(42)
    assert tracking is not None
    assert tracking["recently_played"] == 1
    assert tracking["inactive_since"] is None
    service._steam_presence_poll()
    tracking = store.steam_priority_tracking(42)
    assert tracking["inactive_since"]
    store.close()


def test_active_app_sets_now_playing_and_two_missed_polls_start_decay(tmp_path):
    store, backloggery, service = _setup(tmp_path)
    service.steam.active_app_id = 42
    service._steam_presence_poll()
    tracking = store.steam_priority_tracking(42)
    assert backloggery.row["priority"] == 80
    assert tracking["active_session"] == 1
    assert tracking["missed_presence_polls"] == 0

    service.steam.active_app_id = None
    service._steam_presence_poll()
    tracking = store.steam_priority_tracking(42)
    assert tracking["active_session"] == 1
    assert tracking["missed_presence_polls"] == 1
    assert tracking["inactive_since"] is None

    service._steam_presence_poll()
    tracking = store.steam_priority_tracking(42)
    assert tracking["active_session"] == 0
    assert tracking["inactive_since"]
    assert backloggery.row["priority"] == 80
    store.close()


def test_idle_game_without_observed_recent_playtime_does_not_start_priority_decay(tmp_path):
    store, backloggery, service = _setup(tmp_path, recent=0)
    service._steam_scan()
    assert store.steam_priority_tracking(42) is None
    assert backloggery.row["status"] == 40
    assert backloggery.row["priority"] == 40
    store.close()


def test_steam_scan_dry_run_does_not_write_backloggery(tmp_path):
    store, backloggery, service = _setup(tmp_path, dry_run=True)
    service._steam_scan()
    assert backloggery.updates == []
    assert backloggery.row["status"] == 10
    assert backloggery.row["notes"] == "keep these notes"
    assert "achieve_score" not in backloggery.row
    store.close()


def test_active_owned_game_is_submitted_once_and_linked_under_pc(tmp_path):
    store, backloggery, service = _auto_setup(tmp_path)

    service._steam_presence_poll()
    service._steam_presence_poll()

    candidate = store.get_steam_candidate(42)
    assert candidate["review_status"] == "accepted"
    assert candidate["backloggery_game_inst_id"] == 766
    assert backloggery.added_games[0]["platform_title"] == "PC"
    assert backloggery.created_row["priority"] == 80
    assert len(backloggery.added_games) == 1
    assert service.steam.owned_calls == 1
    store.close()


def test_active_owned_game_links_exact_pc_match_instead_of_creating(tmp_path):
    store, backloggery, service = _auto_setup(tmp_path)
    backloggery.library_rows.append({
        "game_inst_id": 88, "title": "Game", "platform_id": 123,
        "status": 20, "priority": 40, "own": 1, "notes": "Existing Notes",
    })

    service._steam_presence_poll()

    assert store.get_steam_candidate(42)["backloggery_game_inst_id"] == 88
    assert backloggery.added_games == []
    assert backloggery.library_rows[0]["priority"] == 80
    assert backloggery.library_rows[0]["notes"] == "Existing Notes"
    store.close()


def test_active_game_respects_saved_discard(tmp_path):
    store, backloggery, service = _auto_setup(tmp_path)
    store.save_steam_candidates(service.steam.owned_games(), [])
    store.review_steam_candidate(42, "discarded")
    service.steam.owned_calls = 0

    service._steam_presence_poll()

    assert store.get_steam_candidate(42)["review_status"] == "discarded"
    assert backloggery.added_games == []
    assert service.steam.owned_calls == 0
    store.close()


def test_active_non_owned_game_is_not_submitted(tmp_path):
    store, backloggery, service = _auto_setup(tmp_path, active_app_id=999)

    service._steam_presence_poll()

    assert store.get_steam_candidate(999) is None
    assert backloggery.added_games == []
    store.close()


def test_active_game_dry_run_reports_without_accepting_or_submitting(tmp_path):
    store, backloggery, service = _auto_setup(tmp_path, dry_run=True)

    service._steam_presence_poll()

    assert store.get_steam_candidate(42)["review_status"] == "unreviewed"
    assert backloggery.added_games == []
    store.close()
