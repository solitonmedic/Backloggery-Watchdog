import json

import httpx
import pytest
import respx

from backloggery_watchdog.backloggery import BackloggeryClient
from backloggery_watchdog.errors import AuthenticationError, UpstreamError
from backloggery_watchdog.ra import RetroAchievementsClient


@respx.mock
def test_backloggery_rejects_login_html():
    respx.post("https://backloggery.com/api/fetch_user_platforms.php").mock(
        return_value=httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"})
    )
    client = BackloggeryClient("session", "token")
    with pytest.raises(AuthenticationError):
        client.platforms()


@respx.mock
def test_ra_invalid_json_is_upstream_error():
    respx.get("https://retroachievements.org/API/API_GetUserProfile.php").mock(
        return_value=httpx.Response(200, text="broken")
    )
    client = RetroAchievementsClient("key", "user")
    with pytest.raises(UpstreamError):
        client.profile()


def test_backloggery_writes_are_disabled_by_default():
    client = BackloggeryClient("session", "token")
    with pytest.raises(UpstreamError, match="unauthorized endpoint"):
        client.add_game({"title": "Game"})


@respx.mock
def test_live_add_uses_regular_save_and_returns_entry_id():
    route = respx.post("https://backloggery.com/api/add_game.php").mock(
        return_value=httpx.Response(200, json={"msg": "", "payload": "123"})
    )
    client = BackloggeryClient("session", "token", allow_writes=True)
    assert client.add_game({"title": "Game"}) == 123
    body = json.loads(route.calls[0].request.content)
    assert body["is_stealth"] is False
    assert body["update_parent"] is False
    assert body["priority"] == 40


@respx.mock
def test_live_update_posts_preserved_full_object_with_previous_values():
    route = respx.post("https://backloggery.com/api/update_game.php").mock(
        return_value=httpx.Response(200, json={"status": 1, "payload": [{"last_update": "now"}]})
    )
    client = BackloggeryClient("session", "token", allow_writes=True)
    client.update_game({"game_inst_id": 123, "status": 30, "prev_status": 20, "own": 1, "review": "keep"})
    body = json.loads(route.calls[0].request.content)
    assert body["prev_status"] == 20
    assert body["review"] == "keep"
    assert body["is_stealth"] is False


@respx.mock
def test_missing_mapped_game_is_not_misreported_as_expired_session():
    respx.post("https://backloggery.com/api/fetch_gameinfo.php").mock(
        return_value=httpx.Response(200, json={"status": 0, "payload": None})
    )
    client = BackloggeryClient("session", "token")
    assert client.game(999) is None
