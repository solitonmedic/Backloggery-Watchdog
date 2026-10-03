import os
import stat

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


def test_active_watch_requires_three_unchanged_offline_polls(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    assert store.observe(10, "a", True, 3).active is True
    assert store.observe(10, "b", False, 3).stable_polls == 0
    assert store.observe(10, "b", False, 3).stable_polls == 1
    assert store.observe(10, "b", False, 3).stable_polls == 2
    final = store.observe(10, "b", False, 3)
    assert final.stable_polls == 3
    assert final.active is False
