from Backend.helper.combined_migration import migrate_show
from Backend.helper.metadata.parse import apply_combined_override
from Backend.helper.split_files import parse_combined_episodes


def test_combined_stays_on_its_own_season_and_episode():
    payload = {"season_number": 2, "episode_number": 1, "quality": "1080p", "episode_title": "Pilot"}
    apply_combined_override(payload, {"season": 2, "start": 1, "end": 13})
    assert (payload["season_number"], payload["episode_number"], payload["episode_title"]) == (2, 1, "Pilot")
    assert payload["quality"] == "1080p E01-E13"


def test_full_season_pack_is_labelled_full():
    payload = {"season_number": 4, "episode_number": 1, "quality": "720p"}
    apply_combined_override(payload, {"season": 4, "start": None, "end": None})
    assert payload["quality"] == "720p Full"


def test_name_parsing_unchanged():
    assert parse_combined_episodes("Show S02 E01-E13 1080p.mkv") == {"season": 2, "start": 1, "end": 13}
    assert parse_combined_episodes("Show S02 Combined 1080p.mkv") == {"season": 2, "start": None, "end": None}


def _q(name, label="Full"): return {"quality": "1080p " + label, "id": name, "name": name, "size": "1 GB"}


def test_migration_moves_old_pseudo_episode_into_episode_one():
    doc = {"title": "Show", "seasons": [
        {"season_number": 0, "episodes": [{"episode_number": 1002, "title": "Season 2 Combined", "telegram": [_q("a")]}]},
        {"season_number": 2, "episodes": [{"episode_number": 1, "title": "Pilot", "telegram": [_q("b")]}]},
    ]}
    new, moved = migrate_show(doc)
    assert moved == 1
    s2 = next(s for s in new["seasons"] if s["season_number"] == 2)
    ep1 = next(e for e in s2["episodes"] if e["episode_number"] == 1)
    assert [q["id"] for q in ep1["telegram"]] == ["b", "a"]
    assert all(s["season_number"] != 0 for s in new["seasons"])      # emptied season 0 removed


def test_migration_creates_season_and_episode_when_missing_and_is_idempotent():
    doc = {"title": "Show", "seasons": [
        {"season_number": 0, "episodes": [{"episode_number": 1003, "title": "Season 3 Combined", "telegram": [_q("a")]}]}]}
    new, moved = migrate_show(doc)
    assert moved == 1
    assert new["seasons"][0]["season_number"] == 3 and new["seasons"][0]["episodes"][0]["episode_number"] == 1
    again, moved2 = migrate_show(new)
    assert moved2 == 0 and again == new


def test_migration_keeps_real_specials():
    doc = {"title": "Show", "seasons": [{"season_number": 0, "episodes": [{"episode_number": 1, "title": "Special", "telegram": [_q("s")]}]}]}
    new, moved = migrate_show(doc)
    assert moved == 0 and new == doc


def test_migration_puts_a_range_pack_on_its_first_episode():
    doc = {"title": "Show", "seasons": [
        {"season_number": 0, "episodes": [{"episode_number": 1002, "title": "Season 2 Combined",
                                           "telegram": [_q("Show S02 E05-E08 1080p.mkv", "E05-E08"), _q("Show S02 Combined 1080p.mkv")]}]}]}
    new, moved = migrate_show(doc)
    eps = {e["episode_number"]: [q["name"] for q in e["telegram"]] for e in new["seasons"][0]["episodes"]}
    assert moved == 1 and new["seasons"][0]["season_number"] == 2
    assert eps == {5: ["Show S02 E05-E08 1080p.mkv"], 1: ["Show S02 Combined 1080p.mkv"]}
