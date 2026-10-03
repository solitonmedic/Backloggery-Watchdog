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


def test_plain_formatter_uses_plain_language_for_noop():
    record = logging.LogRecord(
        "watchdog",
        logging.INFO,
        "",
        0,
        {"event": "sync_plan", "plan": {"action": "noop", "title": "Kingdom Hearts"}},
        (),
        None,
    )
    assert "Up to date | Kingdom Hearts | no changes needed" in PlainFormatter().format(record)


def test_plain_formatter_displays_rich_presence_on_one_line():
    record = logging.LogRecord(
        "watchdog",
        logging.INFO,
        "",
        0,
        {"event": "rich_presence", "title": "Game", "value": "World: Test\nExpert"},
        (),
        None,
    )
    rendered = PlainFormatter().format(record)
    assert "Rich Presence | Game | World: Test Expert" in rendered
    assert rendered.count("\n") == 0
