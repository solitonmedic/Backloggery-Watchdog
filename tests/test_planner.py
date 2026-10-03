from backloggery_watchdog.models import GameState
from backloggery_watchdog.planner import (
    PLATFORM_MAP,
    REGIONAL_PLATFORM_MAP,
    build_plan,
    resolve_platform_title,
)


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

ACTIVE_RA_SYSTEMS = {
    "Nintendo 64", "Game Boy", "Game Boy Advance", "Game Boy Color", "Sega CD",
    "PlayStation", "Atari Lynx", "Neo Geo Pocket", "Atari Jaguar", "Nintendo DS",
    "Wii", "PlayStation 2", "Magnavox Odyssey 2", "Atari 2600", "Arcade",
    "Virtual Boy", "MSX", "Amstrad CPC", "Apple II", "Dreamcast",
    "PlayStation Portable", "3DO Interactive Multiplayer", "ColecoVision",
    "Intellivision", "Vectrex", "PC-FX", "Atari 7800", "WonderSwan", "Neo Geo CD",
    "Fairchild Channel F", "Watara Supervision", "Arduboy", "WASM-4", "Interton VC 4000",
    "Elektor TV Games Computer", "Atari Jaguar CD", "Nintendo DSi", "Uzebox",
    "Genesis/Mega Drive", "SNES/Super Famicom", "NES/Famicom",
    "PC Engine/TurboGrafx-16", "32X", "Master System", "Game Gear", "GameCube",
    "Pokemon Mini", "SG-1000", "Saturn", "PC-8000/8800", "Mega Duck", "Arcadia 2001",
    "PC Engine CD/TurboGrafx-CD", "Famicom Disk System", "Standalone",
}


def test_every_active_ra_system_has_an_explicit_mapping():
    assert len(ACTIVE_RA_SYSTEMS) == 55
    assert ACTIVE_RA_SYSTEMS == set(PLATFORM_MAP) - {"PlayStation Vita", "Nintendo 3DS", "Wii U"} | set(REGIONAL_PLATFORM_MAP)
    for console in ACTIVE_RA_SYSTEMS:
        assert resolve_platform_title(console, "North America") is not None


def test_missing_platform_and_region_are_blocked():
    assert build_plan(state(console="Dreamcast"), PLATFORMS, [], None, None).action == "blocked"
    assert build_plan(state(region=None), PLATFORMS, [], None, None).action == "blocked"


def test_regional_console_platforms_follow_derived_region():
    expected = {
        "Genesis/Mega Drive": ("Sega Genesis", "Sega Mega Drive", "Sega Mega Drive"),
        "SNES/Super Famicom": (
            "Super Nintendo Entertainment System", "Super Famicom", "Super Nintendo Entertainment System"
        ),
        "NES/Famicom": (
            "Nintendo Entertainment System", "Nintendo Family Computer", "Nintendo Entertainment System"
        ),
        "PC Engine/TurboGrafx-16": ("TurboGrafx-16", "PC Engine", "PC Engine"),
        "PC Engine CD/TurboGrafx-CD": ("TurboGrafx-CD", "PC Engine CD", "PC Engine CD"),
    }
    regions = ("North America", "Japan", "PAL")
    for console, titles in expected.items():
        assert REGIONAL_PLATFORM_MAP[console] == dict(zip(regions, titles, strict=True))
        assert tuple(resolve_platform_title(console, region) for region in regions) == titles
        assert resolve_platform_title(console, None) is None
        assert resolve_platform_title(console, "Asia") is None


def test_approved_fixed_platform_aliases():
    expected = {
        "32X": "Sega 32X",
        "Master System": "Sega Master System",
        "Game Gear": "Sega Game Gear",
        "GameCube": "Nintendo GameCube",
        "Pokemon Mini": "Pokémon Mini",
        "SG-1000": "Sega SG-1000",
        "Saturn": "Sega Saturn",
        "Arcadia 2001": "Emerson Arcadia 2001",
        "Famicom Disk System": "Nintendo Famicom Disk System",
        "PC-8000/8800": "PC-8801",
        "Mega Duck": "Cougar Boy",
        "Standalone": "PC",
    }
    assert {name: PLATFORM_MAP[name] for name in expected} == expected
    assert resolve_platform_title("Unmapped RA System", "North America") is None


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


def test_manual_title_and_region_overrides_do_not_block_ra_notes_or_progress():
    existing = {
        "game_inst_id": 9,
        "title": "Biohazard Outbreak: File #2",
        "platform_id": 132,
        "platform_title": "PlayStation 2",
        "abbr": "PS2",
        "status": 20,
        "notes": "Old presence",
        "phys_digi": 20,
        "own": 1,
        "region": 3,
        "achieve_score": 1,
        "achieve_total": 10,
        "priority": 70,
    }
    plan = build_plan(
        state(rich_presence="New presence", earned=3),
        PLATFORMS,
        [],
        9,
        existing,
        {"title": "Biohazard Outbreak: File #2", "region": 3},
    )
    assert plan.action == "update"
    assert plan.changes["notes"]["to"] == "New presence"
    assert plan.changes["achieve_score"]["to"] == 3
    assert "title" not in plan.changes
    assert "region" not in plan.changes
    assert plan.proposed_payload["priority"] == 70
