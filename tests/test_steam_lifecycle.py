from __future__ import annotations

from backloggery_watchdog.service import WatchdogService
from backloggery_watchdog.state import StateStore

from test_service import config


class FakeRA:
    pass


class FakeSteam:
    def __init__(self, recent=25, active_app_id=None):
        self.recent = recent
        self.active_app_id = active_app_id

    def currently_playing(self):
        return self.active_app_id

    def owned_games(self):
        return [{"appid": 42, "name": "Game", "playtime_forever": 500}]

    def recently_played(self):
        return [{"appid": 42, "playtime_2weeks": self.recent} ] if self.recent else []


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

    def game(self, _entry_id):
        return dict(self.row)

    def update_game(self, payload):
        self.updates.append(dict(payload))
        self.row.update(payload)


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
    assert backloggery.row["notes"] == "keep these notes"
    assert len(backloggery.updates) == 1
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
    store.close()
