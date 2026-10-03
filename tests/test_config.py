import pytest

from backloggery_watchdog.config import Config
from backloggery_watchdog.errors import ConfigurationError


def set_required_environment(monkeypatch):
    monkeypatch.setenv("RA_API_KEY", "key")
    monkeypatch.setenv("PHPSESSID", "session")
    monkeypatch.setenv("log_token", "token")
    monkeypatch.setenv("RA_USERNAME", "user")
    monkeypatch.setenv("BACKLOGGERY_USERNAME", "user")


def test_stealth_save_defaults_to_false(monkeypatch):
    set_required_environment(monkeypatch)
    monkeypatch.delenv("WATCHDOG_STEALTH_SAVE", raising=False)

    assert Config.from_env().stealth_save is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_stealth_save_can_be_enabled_from_environment(monkeypatch, value):
    set_required_environment(monkeypatch)
    monkeypatch.setenv("WATCHDOG_STEALTH_SAVE", value)

    assert Config.from_env().stealth_save is True


def test_stealth_save_rejects_invalid_environment_value(monkeypatch):
    set_required_environment(monkeypatch)
    monkeypatch.setenv("WATCHDOG_STEALTH_SAVE", "sometimes")

    with pytest.raises(ConfigurationError, match="WATCHDOG_STEALTH_SAVE must be true or false"):
        Config.from_env()
