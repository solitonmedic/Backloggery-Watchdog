import logging

from backloggery_watchdog.logging import PlainFormatter, redact


def test_secret_values_are_redacted():
    value = {
        "RA_API_KEY": "secret",
        "nested": {"Cookie": "PHPSESSID=secret"},
        "url": "https://example.test/path?u=user&y=secret&g=1",
    }
    cleaned = redact(value)
    assert "secret" not in str(cleaned)
    assert "[redacted]" in str(cleaned)


def test_plain_formatter_makes_plan_readable():
    record = logging.LogRecord(
        "watchdog",
        logging.INFO,
        "",
        0,
        {
            "event": "sync_plan",
            "plan": {"action": "create", "title": "Kingdom Hearts", "ra_game_id": 32650},
            "summary": "PlayStation 2 | Unfinished | 18/67 achievements | North America | Own | Physical",
        },
        (),
        None,
    )
    rendered = PlainFormatter().format(record)
    assert "Plan CREATE | Kingdom Hearts | RA 32650" in rendered
    assert "18/67 achievements" in rendered
