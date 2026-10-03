from backloggery_watchdog.config import Config
from backloggery_watchdog.service import WatchdogService
from backloggery_watchdog.state import StateStore


class FakeRA:
    def recently_played(self):
        return {
            "GameID": 32650,
            "Title": "Kingdom Hearts",
            "ConsoleName": "PlayStation 2",
            "LastPlayed": "2026-09-28",
            "NumAchieved": 18,
            "NumPossibleAchievements": 67,
        }

    def summary(self, game_id):
        return {"LastGameID": game_id, "RichPresenceMsg": "World: Test", "Status": "Offline"}

    def game_progress(self, game_id):
        return {"NumAchieved": 18, "NumAchievements": 67, "Achievements": {}}

    def game_hashes(self, game_id):
        return [{"Name": "USA"}]


class FakeBackloggery:
    def __init__(self, race_candidate=False):
        self.library_calls = 0
        self.add_calls = 0
        self.race_candidate = race_candidate

    def platforms(self):
        return [{"platform_id": 132, "title": "PlayStation 2", "abbr": "PS2"}]

    def library(self, username):
        self.library_calls += 1
        if self.race_candidate and self.library_calls > 1:
            return [{"game_inst_id": 777, "title": "Kingdom Hearts", "platform_id": 132}]
        return []

    def add_game(self, payload):
        self.add_calls += 1
        return 123

    def game(self, entry_id):
        return {
            "game_inst_id": entry_id,
            "title": "Kingdom Hearts",
            "platform_id": 132,
            "platform_title": "PlayStation 2",
            "abbr": "PS2",
            "status": 20,
            "phys_digi": 20,
            "own": 1,
            "region": 2,
            "achieve_score": 18,
            "achieve_total": 67,
            "notes": "World: Test",
        }


def config(path, dry_run=False):
    return Config(
        ra_api_key="key",
        php_session_id="session",
        log_token="token",
        ra_username="user",
        backloggery_username="user",
        database_path=str(path),
        offline_poll_seconds=900,
        online_poll_seconds=60,
        offline_stable_polls=3,
        log_level="INFO",
        log_format="plain",
        dry_run=dry_run,
    )


def test_live_create_saves_returned_mapping(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    destination = FakeBackloggery()
    result = WatchdogService(config(tmp_path / "state.db"), store, FakeRA(), destination).cycle()
    assert result.plan.action == "create"
    assert destination.add_calls == 1
    assert store.get_mapping(32650) == 123


def test_live_create_is_cancelled_when_prewrite_refresh_finds_candidate(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    destination = FakeBackloggery(race_candidate=True)
    result = WatchdogService(config(tmp_path / "state.db"), store, FakeRA(), destination).cycle()
    assert result.plan.action == "candidate"
    assert destination.add_calls == 0
    assert store.get_mapping(32650) is None


def test_dry_run_never_calls_add(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    destination = FakeBackloggery()
    result = WatchdogService(config(tmp_path / "state.db", dry_run=True), store, FakeRA(), destination).cycle()
    assert result.plan.action == "create"
    assert destination.add_calls == 0
    assert store.get_mapping(32650) is None
