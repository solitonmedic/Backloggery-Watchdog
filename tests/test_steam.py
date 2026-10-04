from __future__ import annotations

import httpx
import pytest

from backloggery_watchdog.errors import UpstreamError
from backloggery_watchdog.steam import (
    SteamClient,
    achievement_counts,
    format_steam_notes,
    latest_unlocked_achievements,
)


def test_owned_and_recent_calls_use_service_input_json():
    requests = []

    def handler(request):
        requests.append(request)
        if "GetOwnedGames" in request.url.path:
            return httpx.Response(
                200,
                json={"response": {"game_count": 1, "games": [{"appid": 42, "name": "Game"}]}},
            )
        return httpx.Response(200, json={"response": {"total_count": 0, "games": []}})

    client = SteamClient("secret", "76561198000000000", httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.owned_games() == [{"appid": 42, "name": "Game"}]
    assert client.recently_played() == []
    assert all(request.url.params["key"] == "secret" for request in requests)
    assert '"include_appinfo":true' in requests[0].url.params["input_json"]
    assert '"steamid":"76561198000000000"' in requests[1].url.params["input_json"]
    assert len(client.request_log) == 2
    assert all(item["request_params"]["key"] == "[REDACTED]" for item in client.request_log)
    assert "secret" not in repr(client.request_log)


def test_resolves_vanity_profile_url():
    def handler(request):
        assert request.url.params["vanityurl"] == "example"
        return httpx.Response(
            200,
            json={"response": {"success": 1, "steamid": "76561198000000000"}},
        )

    client = SteamClient(
        "secret", "https://steamcommunity.com/id/example/", httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.steam_id == "76561198000000000"


def test_missing_owned_games_is_not_treated_as_empty_inventory():
    client = SteamClient(
        "secret",
        "76561198000000000",
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"response": {}}))),
    )
    with pytest.raises(UpstreamError, match="privacy"):
        client.owned_games()


def test_transport_error_does_not_include_api_key():
    def handler(request):
        raise httpx.ConnectError("network failed", request=request)

    client = SteamClient("do-not-log", "76561198000000000", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(UpstreamError) as error:
        client.recently_played()
    assert "do-not-log" not in str(error.value)


def test_recent_achievements_selects_five_newest_unlocked():
    items = [
        {"apiname": f"ACH_{i}", "name": f"Achievement {i}", "achieved": 1, "unlocktime": i}
        for i in range(1, 8)
    ]
    items.extend([
        {"apiname": "LOCKED", "name": "Locked", "achieved": 0, "unlocktime": 99},
        {"apiname": "NO_TIME", "name": "No time", "achieved": 1, "unlocktime": 0},
    ])
    assert latest_unlocked_achievements(items) == [
        "Achievement 7", "Achievement 6", "Achievement 5", "Achievement 4", "Achievement 3"
    ]


def test_achievement_counts_counts_unlocked_and_locked_records():
    assert achievement_counts(
        [{"achieved": 1}, {"achieved": "1"}, {"achieved": 0}, {}]
    ) == (2, 4)
    assert achievement_counts([]) == (0, 0)


def test_steam_notes_use_requested_markdown_and_empty_states():
    assert format_steam_notes(424, 831, ["A", "B"]) == (
        "**Recent playtime:** *424 minutes*\n"
        "**Lifetime playtime:** *831 minutes*\n"
        "**Recent achievements:** *A*, *B*"
    )
    assert "*None recorded*" in format_steam_notes(0, 0, [])
    assert "*Unavailable*" in format_steam_notes(0, 0, None)


def test_player_achievements_reads_v1_endpoint():
    def handler(request):
        assert request.url.path.endswith("ISteamUserStats/GetPlayerAchievements/v1/")
        assert request.url.params["appid"] == "42"
        return httpx.Response(200, json={"playerstats": {"success": True, "achievements": []}})

    client = SteamClient("secret", "76561198000000000", httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.player_achievements(42) == []


def test_currently_playing_reads_gameid_from_player_summaries_v2():
    def handler(request):
        assert request.url.path.endswith("ISteamUser/GetPlayerSummaries/v2/")
        assert request.url.params["steamids"] == "76561198000000000"
        return httpx.Response(
            200,
            json={"response": {"players": [{"gameid": "42", "gameextrainfo": "Game"}]}},
        )

    client = SteamClient(
        "secret", "76561198000000000", httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert client.currently_playing() == 42


def test_currently_playing_returns_none_when_summary_has_no_active_app():
    client = SteamClient(
        "secret",
        "76561198000000000",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"response": {"players": [{"personastate": 1}]}}
                )
            )
        ),
    )
    assert client.currently_playing() is None
