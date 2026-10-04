import os
import stat

import pytest

from backloggery_watchdog.state import StateStore


def test_mapping_persists_across_reopen(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    store.set_mapping(10, 20)
    store.close()
    reopened = StateStore(path)
    assert reopened.get_mapping(10) == 20


def test_state_database_respects_private_process_umask(tmp_path):
    previous = os.umask(0o077)
    try:
        path = tmp_path / "state.db"
        StateStore(str(path)).close()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(previous)


def test_existing_state_database_is_tightened_to_owner_only(tmp_path):
    path = tmp_path / "state.db"
    path.touch(mode=0o644)
    StateStore(str(path)).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_title_and_region_mismatches_are_preserved_as_first_observation_overrides(tmp_path):
    path = str(tmp_path / "state.db")
    store = StateStore(path)
    overrides = store.reconcile_field_overrides(
        10,
        {"title": "Biohazard Outbreak File #2", "region": "3"},
        {"title": "Resident Evil Outbreak: File #2", "region": 2},
    )
    assert overrides == {"title": "Biohazard Outbreak File #2", "region": "3"}
    store.close()
    reopened = StateStore(path)
    assert reopened.get_field_overrides(10) == overrides


def test_external_edit_updates_override_and_clear_reenables_ra_control(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    desired = {"title": "RA title", "region": 2}
    assert store.reconcile_field_overrides(10, desired, desired) == {}
    assert store.reconcile_field_overrides(
        10, {"title": "Manual title", "region": 2}, desired
    ) == {"title": "Manual title"}
    assert store.reconcile_field_overrides(
        10, {"title": "Another manual title", "region": "2"}, desired
    ) == {"title": "Another manual title"}
    store.clear_field_overrides(10, "title")
    assert store.get_field_overrides(10) == {}


def test_returning_a_field_to_ra_value_clears_its_override(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    desired = {"title": "RA title", "region": 2}
    store.reconcile_field_overrides(10, {"title": "Manual title", "region": 2}, desired)
    assert store.reconcile_field_overrides(10, desired, desired) == {}


def test_null_region_can_be_preserved_as_an_explicit_override(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    overrides = store.reconcile_field_overrides(
        10, {"title": "RA title", "region": None}, {"title": "RA title", "region": 2}
    )
    assert "region" in overrides
    assert overrides["region"] is None


def test_active_watch_requires_three_unchanged_offline_polls(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    assert store.observe(10, "a", True, 3).active is True
    assert store.observe(10, "b", False, 3).stable_polls == 0
    assert store.observe(10, "b", False, 3).stable_polls == 1
    assert store.observe(10, "b", False, 3).stable_polls == 2
    final = store.observe(10, "b", False, 3)
    assert final.stable_polls == 3
    assert final.active is False


def test_steam_candidates_update_playtime_but_preserve_review_status(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    first = store.save_steam_candidates(
        [{"appid": 42, "name": "Game", "playtime_forever": 120}],
        [{"appid": 42, "playtime_2weeks": 30}],
    )
    assert first == {"new": 1, "changed": 0, "seen": 1}
    store.db.execute("UPDATE steam_candidates SET review_status='accepted' WHERE steam_appid=42")
    store.db.commit()
    second = store.save_steam_candidates(
        [{"appid": 42, "name": "Game", "playtime_forever": 180}],
        [{"appid": 42, "playtime_2weeks": 45}],
    )
    candidate = store.list_steam_candidates(1)[0]
    assert second == {"new": 0, "changed": 1, "seen": 1}
    assert candidate["playtime_forever"] == 180
    assert candidate["playtime_recent"] == 45
    assert candidate["review_status"] == "accepted"


def test_steam_candidate_review_and_details_persist(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_steam_candidates([{"appid": 42, "name": "Game", "playtime_forever": 831}], [])
    store.set_steam_candidate_details(
        42,
        "**Recent playtime:** *424 minutes*",
        ["Achievement"],
        "available",
        3,
        8,
    )
    store.review_steam_candidate(
        42,
        "accepted_with_edits",
        canonical_title="Canonical Game",
        platform="PC",
        subsystem="PlayStation 2",
        review_notes="Verified as a remaster",
    )
    candidate = store.get_steam_candidate(42)
    assert candidate["review_status"] == "accepted_with_edits"
    assert candidate["canonical_title"] == "Canonical Game"
    assert candidate["platform"] == "PC"
    assert candidate["subsystem"] == "PlayStation 2"
    assert candidate["review_notes"] == "Verified as a remaster"
    assert candidate["notes"] == "**Recent playtime:** *424 minutes*"
    assert candidate["achievements_json"] == '["Achievement"]'
    assert candidate["achievements_earned"] == 3
    assert candidate["achievements_total"] == 8
    assert store.list_steam_candidates(status="unreviewed") == []


def test_steam_candidate_details_migrate_existing_schema(tmp_path):
    import sqlite3

    path = tmp_path / "old-state.db"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE steam_candidate_details (
           steam_appid INTEGER PRIMARY KEY, notes TEXT NOT NULL,
           achievements_json TEXT NOT NULL, achievement_status TEXT NOT NULL,
           updated_at TEXT NOT NULL)"""
    )
    db.execute(
        "INSERT INTO steam_candidate_details VALUES (42, 'notes', '[]', 'available', 'now')"
    )
    db.commit()
    db.close()

    store = StateStore(str(path))
    store.save_steam_candidates([{"appid": 42, "name": "Game"}], [])
    assert store.get_steam_candidate(42)["achievements_earned"] is None
    store.set_steam_candidate_details(42, "notes", [], "available", 1, 2)
    assert store.get_steam_candidate(42)["achievements_total"] == 2


def test_review_requires_existing_candidate_and_edits(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    with pytest.raises(ValueError, match="unknown Steam AppID"):
        store.review_steam_candidate(42, "accepted")
    store.save_steam_candidates([{"appid": 42, "name": "Game"}], [])
    with pytest.raises(ValueError, match="at least one edited field"):
        store.review_steam_candidate(42, "accepted_with_edits")


def test_discarded_steam_candidate_stays_discarded_after_next_library_scan(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_steam_candidates([{"appid": 42, "name": "Game", "playtime_forever": 10}], [])
    store.review_steam_candidate(42, "discarded")

    store.save_steam_candidates(
        [{"appid": 42, "name": "Updated Game Name", "playtime_forever": 15}], []
    )

    candidate = store.get_steam_candidate(42)
    assert candidate["review_status"] == "discarded"
    assert candidate["steam_name"] == "Updated Game Name"
    assert store.list_steam_candidates(status="unreviewed") == []
    assert len(store.list_steam_candidates(status="discarded")) == 1
