from __future__ import annotations

import httpx
import pytest

from backloggery_watchdog.backloggery import BackloggeryClient
from backloggery_watchdog.state import StateStore
from backloggery_watchdog.steam import SteamClient
from backloggery_watchdog.steam_submit import submit_steam_candidate


class FakeBackloggery:
    def __init__(self, library=None):
        self.registered = []
        self.library_rows = list(library or [])
        self.added_platforms = []
        self.added_games = []
        self.catalog = [{"platform_id": 123, "title": "PC", "abbr": "PC", "format": 3}]

    def platforms(self):
        return list(self.registered)

    def platform_catalog(self):
        return self.catalog

    def add_platform(self, platform):
        self.added_platforms.append(platform)
        self.registered.append(platform)

    def library(self, _username):
        return list(self.library_rows)

    def add_game(self, payload):
        self.added_games.append(payload)
        return 765

    def game(self, entry_id):
        return next(
            (row for row in self.library_rows if row.get("game_inst_id") == entry_id),
            {"game_inst_id": entry_id, **self.added_games[-1]},
        )


def _candidate_store(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_steam_candidates(
        [{"appid": 42, "name": "Game", "playtime_forever": 831}],
        [{"appid": 42, "playtime_2weeks": 424}],
    )
    store.review_steam_candidate(42, "accepted")
    return store


def _steam_client(unlocked=1, total=2):
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "playerstats": {
                    "success": True,
                    "achievements": [
                        {"name": "Unlocked", "achieved": 1, "unlocktime": 5},
                        *[
                            {"name": f"Other {index}", "achieved": int(index < unlocked), "unlocktime": 5 if index < unlocked else 0}
                            for index in range(1, total)
                        ],
                    ],
                }
            },
        )

    return SteamClient(
        "test-key",
        "76561198000000000",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_submit_adds_approved_candidate_to_registered_pc_with_notes(tmp_path):
    store = _candidate_store(tmp_path)
    backloggery = FakeBackloggery()
    steam = _steam_client()

    result = submit_steam_candidate(store, backloggery, steam, 42, "user")

    assert backloggery.added_platforms == [backloggery.catalog[0]]
    payload = backloggery.added_games[0]
    assert payload["title"] == "Game"
    assert payload["status"] == 20
    assert payload["priority"] == 80
    assert payload["platform_id"] == 123
    assert payload["platform_title"] == "PC"
    assert payload["achieve_score"] == 1
    assert payload["achieve_total"] == 2
    assert payload["notes"] == (
        "**Recent playtime:** *7 hours and 4 minutes*\n"
        "**Lifetime playtime:** *13 hours and 51 minutes*\n"
        "**Recent achievements:** *Unlocked*"
    )
    assert result == {"appid": 42, "action": "created", "game_inst_id": 765}
    assert store.get_steam_candidate(42)["backloggery_game_inst_id"] == 765


def test_submit_marks_all_achievements_completed(tmp_path):
    store = _candidate_store(tmp_path)
    backloggery = FakeBackloggery()
    submit_steam_candidate(store, backloggery, _steam_client(unlocked=2, total=2), 42, "user")
    assert backloggery.added_games[0]["status"] == 40


def test_submit_links_exact_pc_match_without_duplicate_or_overwrite(tmp_path):
    store = _candidate_store(tmp_path)
    existing = {
        "game_inst_id": 88,
        "title": "Game",
        "platform_id": 123,
        "notes": "Existing user Notes",
    }
    backloggery = FakeBackloggery([existing])
    backloggery.registered.append(backloggery.catalog[0])
    steam = _steam_client()

    result = submit_steam_candidate(store, backloggery, steam, 42, "user")

    assert result == {"appid": 42, "action": "linked_existing", "game_inst_id": 88}
    assert backloggery.added_games == []
    assert existing["notes"] == "Existing user Notes"
    assert store.get_steam_candidate(42)["backloggery_game_inst_id"] == 88


def test_submit_refuses_unaccepted_candidates(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_steam_candidates([{"appid": 42, "name": "Game"}], [])
    with pytest.raises(ValueError, match="accepted"):
        submit_steam_candidate(store, FakeBackloggery(), _steam_client(), 42, "user")


def test_linked_submission_is_idempotent_and_does_not_call_upstreams(tmp_path):
    store = _candidate_store(tmp_path)
    store.set_steam_candidate_backloggery_link(42, 765)

    class NeverCalled:
        def __getattr__(self, _name):
            raise AssertionError("already-submitted candidate called an upstream")

    assert submit_steam_candidate(store, NeverCalled(), NeverCalled(), 42, "user") == {
        "appid": 42,
        "action": "already_submitted",
        "game_inst_id": 765,
    }


def test_live_backloggery_add_game_payload_uses_pc_and_achievement_counts():
    import json

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"payload": "765"})

    client = BackloggeryClient("session", "token", allow_writes=True, transport=httpx.MockTransport(handler))
    assert client.add_game(
        {
            "title": "Game",
            "platform_id": 123,
            "platform_title": "PC",
            "achieve_score": 1,
            "achieve_total": 2,
        }
    ) == 765
    assert seen[0]["platform_id"] == 123
    assert seen[0]["achieve_score"] == 1
    assert seen[0]["achieve_total"] == 2
