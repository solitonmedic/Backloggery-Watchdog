from __future__ import annotations

from backloggery_watchdog.cli import _parser, _steam_review_queue
from backloggery_watchdog.state import StateStore


def test_interactive_review_remembers_accept_discard_and_session_skip(tmp_path, monkeypatch, capsys):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_steam_candidates(
        [
            {"appid": 1, "name": "Accept me", "playtime_forever": 30},
            {"appid": 2, "name": "Discard me", "playtime_forever": 20},
            {"appid": 3, "name": "Skip me", "playtime_forever": 10},
            {"appid": 4, "name": "Leave for later", "playtime_forever": 5},
        ],
        [],
    )
    choices = iter(["a", "d", "s", "q"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(choices))

    assert _steam_review_queue(store, limit=20) == 0

    assert store.get_steam_candidate(1)["review_status"] == "accepted"
    assert store.get_steam_candidate(2)["review_status"] == "discarded"
    assert store.get_steam_candidate(3)["review_status"] == "unreviewed"
    assert store.get_steam_candidate(4)["review_status"] == "unreviewed"
    output = capsys.readouterr().out
    assert "Discarded; this choice is saved across future Steam imports." in output
    assert "Review stopped; unvisited candidates remain unreviewed." in output


def test_review_without_arguments_selects_interactive_queue():
    args = _parser().parse_args(["steam", "review"])
    assert args.app_id is None
    assert args.decision is None
    assert args.interactive is False


def test_steam_submit_command_selects_batch_or_single_candidate():
    batch = _parser().parse_args(["steam", "submit"])
    single = _parser().parse_args(["steam", "submit", "2552440"])
    assert batch.app_id is None
    assert single.app_id == 2552440


def test_interactive_accept_submits_after_user_approval(tmp_path, monkeypatch, capsys):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_steam_candidates([{"appid": 1, "name": "Accept me"}], [])
    submitted = []
    monkeypatch.setenv("WATCHDOG_DRY_RUN", "false")
    monkeypatch.setattr(
        "backloggery_watchdog.cli._submit_approved_candidate",
        lambda _store, app_id: submitted.append(app_id) or {"game_inst_id": 123},
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "a")

    assert _steam_review_queue(store, limit=1) == 0
    assert submitted == [1]
    assert "Accepted and submitted to Backloggery under PC." in capsys.readouterr().out


def test_interactive_review_displays_achievement_count(tmp_path, monkeypatch, capsys):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_steam_candidates([{"appid": 42, "name": "Game"}], [])
    store.set_steam_candidate_details(42, "notes", [], "available", 7, 12)
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")

    assert _steam_review_queue(store, limit=1) == 0
    assert "Steam achievements: 7/12 earned" in capsys.readouterr().out
