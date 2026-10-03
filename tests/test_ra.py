from backloggery_watchdog.ra import derive_region, normalize_game_state


def test_region_priority_uses_all_hash_labels():
    hashes = [{"Name": "Germany"}, {"Name": "USA"}]
    assert derive_region(hashes) == "North America"


def test_rich_presence_requires_matching_last_game():
    recent = {"GameID": 10, "Title": "Game", "ConsoleName": "PlayStation", "LastPlayed": "now", "NumAchieved": 1, "NumPossibleAchievements": 2}
    progress = {"NumAchieved": 1, "NumAchievements": 2, "Achievements": {}}
    matching = normalize_game_state(recent, {"LastGameID": 10, "RichPresenceMsg": "Here", "Status": "Offline"}, progress, [{"Name": "USA"}])
    mismatch = normalize_game_state(recent, {"LastGameID": 11, "RichPresenceMsg": "Wrong", "Status": "Offline"}, progress, [{"Name": "USA"}])
    assert matching.rich_presence == "Here"
    assert mismatch.rich_presence is None


def test_win_condition_and_mastery_are_metadata_driven():
    recent = {"GameID": 10, "Title": "Game", "ConsoleName": "PlayStation", "LastPlayed": "now"}
    progress = {
        "NumAchieved": 2,
        "NumAchievements": 3,
        "Achievements": {"1": {"Type": "win_condition", "DateEarned": "2026-01-01"}},
    }
    state = normalize_game_state(recent, {"LastGameID": 10, "Status": "Offline"}, progress, [{"Name": "Japan"}])
    assert state.beaten is True
    assert state.mastered is False
    assert state.status == "Beaten"


def test_subset_title_is_removed_from_game_title_and_added_once_to_notes():
    recent = {
        "GameID": 10,
        "Title": "Resident Evil Outbreak [Subset - Online Multiplayer]",
        "ConsoleName": "PlayStation 2",
        "LastPlayed": "now",
    }
    state = normalize_game_state(
        recent,
        {"LastGameID": 10, "RichPresenceMsg": "Lobby | Chris", "Status": "Online"},
        {"NumAchieved": 0, "NumAchievements": 10},
        [],
        {"ParentGameID": 100},
    )
    assert state.title == "Resident Evil Outbreak"
    assert state.subset_title == "Online Multiplayer"
    assert state.region == "North America"
    assert state.notes == "Lobby | Chris [Subset - Online Multiplayer]"


def test_subset_uses_hash_region_when_available_and_preserves_notes_without_presence():
    recent = {"GameID": 10, "Title": "Game [Subset - Extra Mode]", "ConsoleName": "PlayStation 2"}
    state = normalize_game_state(
        recent,
        {"LastGameID": 10, "RichPresenceMsg": None},
        {"NumAchievements": 10},
        [{"Name": "Game (Japan).iso"}],
        {"ParentGameID": 100},
    )
    assert state.region == "Japan"
    assert state.notes is None


def test_japanese_hash_title_overrides_ra_display_title():
    recent = {"GameID": 10, "Title": "Resident Evil Outbreak [Subset - Online Multiplayer]", "ConsoleName": "PlayStation 2"}
    hashes = {"Results": [
        {"Name": "Biohazard Outbreak (Japan).iso"},
        {"Name": "Biohazard Outbreak (Japan) (Rev 1).iso"},
        {"Name": "Resident Evil Outbreak (USA).iso"},
    ]}
    state = normalize_game_state(
        recent,
        {"LastGameID": 10, "RichPresenceMsg": "Online"},
        {"NumAchievements": 10},
        hashes,
        {"ParentGameID": 100},
    )
    assert state.title == "Biohazard Outbreak"
    assert state.region == "North America"
    assert state.notes == "Online [Subset - Online Multiplayer]"


def test_conflicting_japanese_hash_titles_keep_ra_title_and_report_warning():
    recent = {"GameID": 10, "Title": "Game", "ConsoleName": "PlayStation 2"}
    state = normalize_game_state(
        recent,
        {"LastGameID": 10},
        {"NumAchievements": 10},
        {"Results": [{"Name": "Game A (Japan).iso"}, {"Name": "Game B (Japan).iso"}]},
    )
    assert state.title == "Game"
    assert state.title_resolution_warning == "conflicting Japanese hash titles; retained RA game title"


def test_non_subset_without_region_remains_unresolved():
    recent = {"GameID": 10, "Title": "Game", "ConsoleName": "PlayStation 2"}
    state = normalize_game_state(recent, {}, {}, [])
    assert state.region is None
