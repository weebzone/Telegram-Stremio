from Backend.helper.stream_badges import badge_lines


def test_quality_line_is_ordered_and_short():
    quality, langs = badge_lines("Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR10.HEVC.TrueHD.Atmos.7.1.ITA.ENG-GROUP.mkv")
    assert quality == "4K · DV · HDR10 · REMUX · Atmos · TrueHD · 7.1 · HEVC"
    assert langs == "English · Italian"


def test_indian_webrip_with_languages():
    quality, langs = badge_lines("You (2018) S01 COMBINED NF WEBRiP 720P 10bit HEVC [Hindi DD5.1 + English].mkv")
    assert quality == "720p · WEBRip · DD · 5.1 · HEVC"      # bit depth is noise, left out
    assert langs == "English · Hindi"


def test_bad_copies_are_flagged():
    quality, _ = badge_lines("Aadarsha Kutumbam House No 47 (2026) Telugu PreDVD - 1080p.mkv")
    assert quality == "1080p · ⚠ Telesync"


def test_nothing_recognised_gives_empty_lines():
    assert badge_lines("home video.mp4") == ("", "")


def test_underscore_names_keep_channel_counts():
    quality, langs = badge_lines("You_2018_S02_COMBINED_NF_WEBRiP_720P_10bit_HEVC_Hindi_DD5_1_+_English.mkv")
    assert quality == "720p · WEBRip · DD · 5.1 · HEVC" and langs == "English · Hindi"


from Backend.helper.stream_badges import badge_images


def test_image_badges_use_this_server_and_pack_order():
    badges = badge_images("Dune.2024.2160p.BluRay.REMUX.DV.HEVC.Atmos.7.1.Hindi.mkv", "https://tv.example.com")
    names = [b["name"] for b in badges]
    assert names[:2] == ["4K", "Dolby Vision"] and "Hindi" in names
    assert all(b["imageURL"].startswith("https://tv.example.com/badges/prism/") for b in badges)
