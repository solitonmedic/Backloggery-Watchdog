from backloggery_watchdog.config import Config
from backloggery_watchdog.service import WatchdogService
from backloggery_watchdog.state import StateStore


class FakeRA:
    def __init__(self, console="PlayStation 2"):
        self.console = console

    def recently_played(self):
        return {
            "GameID": 32650,
            "Title": "Kingdom Hearts",
            "ConsoleName": self.console,
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
    def __init__(self, race_candidate=False, platform_present=True):
        self.library_calls = 0
        self.add_calls = 0
        self.platform_add_calls = 0
        self.race_candidate = race_candidate
        self.platform_present = platform_present
        self.current_platform = {
            "platform_id": 132,
            "title": "PlayStation 2",
            "abbr": "PS2",
            "format": 3,
        }
        self.last_game_payload = None

    def platforms(self):
        return [self.current_platform] if self.platform_present else []

    def platform_catalog(self):
        return [{"platform_id": 40, "title": "Dreamcast", "abbr": "DC", "format": 3}]

    def add_platform(self, platform):
        self.platform_add_calls += 1
        self.current_platform = dict(platform)
        self.platform_present = True

    def library(self, username):
        self.library_calls += 1
        if self.race_candidate and self.library_calls > 1:
            return [{"game_inst_id": 777, "title": "Kingdom Hearts", "platform_id": 132}]
        return []

    def add_game(self, payload):
        self.add_calls += 1
        self.last_game_payload = dict(payload)
        return 123

    def game(self, entry_id):
        return {"game_inst_id": entry_id, **self.last_game_payload}


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


def test_live_cycle_registers_missing_platform_then_adds_game(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    destination = FakeBackloggery(platform_present=False)
    result = WatchdogService(
        config(tmp_path / "state.db"), store, FakeRA(console="Dreamcast"), destination
    ).cycle()
    assert result.plan.action == "create"
    assert destination.platform_add_calls == 1
    assert destination.current_platform["title"] == "Dreamcast"
    assert destination.add_calls == 1
    assert store.get_mapping(32650) == 123


def test_dry_run_reports_missing_platform_without_registering_it(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    destination = FakeBackloggery(platform_present=False)
    result = WatchdogService(
        config(tmp_path / "state.db", dry_run=True),
        store,
        FakeRA(console="Dreamcast"),
        destination,
    ).cycle()
    assert result.plan.action == "platform_required"
    assert destination.platform_add_calls == 0
    assert destination.add_calls == 0
