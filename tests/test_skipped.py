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


import pytest

from Backend.helper import skipped_files


class _Client:
    def __init__(self, message): self.message = message
    async def get_messages(self, chat, msg_id): return self.message


def _doc_message(file_name="ep.mkv", caption=None, height=0):
    doc = NS(file_name=file_name, file_size=2048, height=height, mime_type="video/x-matroska")
    return NS(chat=NS(id=-1001234), id=77, caption=caption, video=None, document=doc, empty=False, entities=None, caption_entities=None)


@pytest.fixture
def fake_db(monkeypatch):
    calls = {}
    record = {"_id": "1234:77", "chat_id": "1234", "msg_id": 77, "attempts": 0, "anime": True}

    async def get(_): return dict(record)
    async def set_(sid, **kw): calls["set"] = kw; return True
    async def insert(info, **kw): calls["insert"] = (info, kw); return "id1"
    monkeypatch.setattr(skipped_files.db, "get_skipped_file", get)
    monkeypatch.setattr(skipped_files.db, "set_skipped_file", set_)
    monkeypatch.setattr(skipped_files.db, "insert_media", insert)

    async def noop(*a, **k): return None
    monkeypatch.setattr(skipped_files, "apply_video_thumb_to_metadata", noop)
    monkeypatch.setattr("Backend.helper.auto_catalog.start_single_media_catalog_sync", lambda *a, **k: None)
    return calls


async def test_retry_with_typed_title_on_a_document_without_height_still_gets_a_quality(monkeypatch, fake_db):
    seen = {}

    async def fake_metadata(filename, channel, msg_id, **kw):
        seen.update(filename=filename, **kw)
        return {"title": "Breaking Bad", "media_type": "tv", "tmdb_id": 1, "encoded_string": "x"}
    monkeypatch.setattr(skipped_files, "metadata", fake_metadata)

    r = await skipped_files.retry_skipped(_Client(_doc_message()), "1234:77", title="Breaking Bad S02E05")
    assert r["ok"] is True
    assert seen["fallback_quality"] == "HD"        # no height, no resolution in either name -> plain label, not a failure
    assert seen["force_anime"] is True             # remembered from the original channel
    assert "Breaking" in seen["filename"] and fake_db["insert"][1]["channel"] == 1234


async def test_retry_prefers_real_height_then_original_name(monkeypatch, fake_db):
    seen = {}

    async def fake_metadata(filename, channel, msg_id, **kw):
        seen.update(kw); return None
    monkeypatch.setattr(skipped_files, "metadata", fake_metadata)

    await skipped_files.retry_skipped(_Client(_doc_message(height=1080)), "1234:77")
    assert seen["fallback_quality"] == "1080p"
    await skipped_files.retry_skipped(_Client(_doc_message(file_name="Show.S01E01.720p.mkv")), "1234:77", title="Show S01E01")
    assert seen["fallback_quality"] == "720p"      # typed title has no resolution; the original name did


async def test_failed_retry_keeps_the_record_and_updates_reason(monkeypatch, fake_db):
    async def none(*a, **k): return None
    monkeypatch.setattr(skipped_files, "metadata", none)
    r = await skipped_files.retry_skipped(_Client(_doc_message()), "1234:77")
    assert r["ok"] is False and fake_db["set"]["attempts"] == 1 and "insert" not in fake_db
