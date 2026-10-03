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
