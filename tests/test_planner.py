from backloggery_watchdog.models import GameState
from backloggery_watchdog.planner import build_plan


def state(**changes):
    values = dict(
        ra_game_id=10,
        title="Game",
        console="PlayStation 2",
        last_played="now",
        rich_presence="Place",
        online=False,
        earned=2,
        total=10,
        beaten=False,
        mastered=False,
        region="North America",
    )
    values.update(changes)
    return GameState(**values)


PLATFORMS = [{"platform_id": 132, "title": "PlayStation 2", "abbr": "PS2"}]


def test_missing_platform_and_region_are_blocked():
    assert build_plan(state(console="Dreamcast"), PLATFORMS, [], None, None).action == "blocked"
    assert build_plan(state(region=None), PLATFORMS, [], None, None).action == "blocked"


def test_exact_candidate_is_not_automatically_mapped():
    library = [{"game_inst_id": 9, "title": "game", "platform_id": 132}]
    plan = build_plan(state(), PLATFORMS, library, None, None)
    assert plan.action == "candidate"
    assert plan.candidate_entry_ids == [9]


def test_update_preserves_unowned_fields_and_special_status():
    existing = {
        "game_inst_id": 9,
        "title": "Game",
        "platform_id": 132,
        "platform_title": "PlayStation 2",
        "abbr": "PS2",
        "status": 50,
        "priority": 70,
        "notes": "Old",
        "phys_digi": 10,
        "own": 2,
        "region": 2,
        "achieve_score": 1,
        "achieve_total": 10,
        "review": "Keep me",
    }
    plan = build_plan(state(), PLATFORMS, [], 9, existing)
    assert plan.action == "update"
    assert plan.proposed_payload["priority"] == 70
    assert plan.proposed_payload["review"] == "Keep me"
    assert plan.proposed_payload["status"] == 50
    assert "priority" not in plan.changes


def test_missing_rich_presence_preserves_notes():
    existing = {
        "game_inst_id": 9, "title": "Game", "platform_id": 132, "platform_title": "PlayStation 2",
        "abbr": "PS2", "status": 20, "notes": "Keep", "phys_digi": 20, "own": 1,
        "region": 2, "achieve_score": 2, "achieve_total": 10,
    }
    plan = build_plan(state(rich_presence=None), PLATFORMS, [], 9, existing)
    assert plan.action == "noop"


def test_numeric_strings_do_not_create_false_changes():
    existing = {
        "game_inst_id": 9, "title": "Game", "platform_id": "132", "platform_title": None,
        "abbr": None, "status": "20", "notes": "Place", "phys_digi": "20", "own": "1",
        "region": "2", "achieve_score": "2", "achieve_total": "10",
    }
    assert build_plan(state(), PLATFORMS, [], 9, existing).action == "noop"
