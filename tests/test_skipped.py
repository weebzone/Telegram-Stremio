from types import SimpleNamespace as NS

from Backend.helper.manual_add import quality_from_height
from Backend.helper.skipped_files import build_doc


def _msg(file_name="x.mkv", caption=None, height=0):
    return NS(chat=NS(id=-1001234), id=77, caption=caption, video=None,
              document=NS(file_name=file_name, file_size=5, height=height))


def test_doc_identity_and_defaults():
    d = build_doc(_msg("Some.Movie.mkv"))
    assert d["_id"] == "1234:77" and d["chat_id"] == "1234" and d["msg_id"] == 77
    assert "quality" in d["reason"].lower()      # name has no resolution -> reason says so


def test_reason_prefers_caption_over_file_name():
    d = build_doc(_msg("a.mkv", caption="Show S01E01 1080p"))
    assert "quality" not in d["reason"].lower()


def test_quality_from_video_height():
    assert quality_from_height(1080) == "1080p" and quality_from_height(720) == "720p" and quality_from_height(0) == ""
