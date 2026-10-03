from backloggery_watchdog.logging import redact


def test_secret_values_are_redacted():
    value = {
        "RA_API_KEY": "secret",
        "nested": {"Cookie": "PHPSESSID=secret"},
        "url": "https://example.test/path?u=user&y=secret&g=1",
    }
    cleaned = redact(value)
    assert "secret" not in str(cleaned)
    assert "[redacted]" in str(cleaned)
