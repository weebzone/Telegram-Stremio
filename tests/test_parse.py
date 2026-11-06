import pytest

from Backend.helper.metadata.parse import extract_absolute_episode, parse_media_name


@pytest.mark.parametrize("name,title,year,season,episode", [
    ("Inception.2010.1080p.BluRay.x264.mkv", "Inception", 2010, None, None),
    ("Breaking.Bad.S02E05.720p.WEB-DL.mkv", "Breaking Bad", None, 2, 5),
    ("The Office US S03 E10 480p.mkv", "The Office US", None, 3, 10),
    ("Dark (2017) S01E01 Secrets 1080p.mkv", "Dark", 2017, 1, 1),
    ("Movie Title (2019) 2160p.mkv", "Movie Title", 2019, None, None),
    ("Friends.S04.COMPLETE.720p.mkv", "Friends", None, 4, None),
])
def test_parse_basic(name, title, year, season, episode):
    p = parse_media_name(name)
    assert (p["title"], p["year"], p["season"], p["episode"]) == (title, year, season, episode)


def test_multi_episode_is_a_list():
    p = parse_media_name("Show.Name.S01E01-E03.1080p.mkv")
    assert p["season"] == 1 and p["episode"] == [1, 2, 3]


@pytest.mark.parametrize("name,expected", [
    ("One Piece - 1172 [1080p].mkv", 1172),
    ("Naruto Shippuden 220 720p.mkv", 220),
])
def test_absolute_episode_not_split_into_season(name, expected):
    # guessit would read 1172 as S11E72; the parser must keep it as an absolute episode
    p = parse_media_name(name)
    assert p["season"] is None
    assert extract_absolute_episode(name, p) == expected
