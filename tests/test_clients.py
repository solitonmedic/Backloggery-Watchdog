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


def test_backloggery_client_has_no_write_methods():
    assert not hasattr(BackloggeryClient, "add_game")
    assert not hasattr(BackloggeryClient, "update_game")
    assert not hasattr(BackloggeryClient, "add_platform")


@respx.mock
def test_missing_mapped_game_is_not_misreported_as_expired_session():
    respx.post("https://backloggery.com/api/fetch_gameinfo.php").mock(
        return_value=httpx.Response(200, json={"status": 0, "payload": None})
    )
    client = BackloggeryClient("session", "token")
    assert client.game(999) is None
