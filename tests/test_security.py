import zlib

import pytest

from Backend.helper import login_guard
from Backend.helper.encrypt import base62_encode, decode_string, encode_string


def test_login_blocked_after_five_failures_and_cleared_on_success():
    login_guard.reset()
    for _ in range(5):
        assert not login_guard.is_blocked("1.2.3.4")
        login_guard.record_failure("1.2.3.4")
    assert login_guard.is_blocked("1.2.3.4")
    assert not login_guard.is_blocked("5.6.7.8")      # other IPs unaffected
    login_guard.record_success("1.2.3.4")
    assert not login_guard.is_blocked("1.2.3.4")


def test_login_block_expires(monkeypatch):
    login_guard.reset()
    t = [1000.0]
    monkeypatch.setattr(login_guard, "_now", lambda: t[0])
    for _ in range(5):
        login_guard.record_failure("9.9.9.9")
    assert login_guard.is_blocked("9.9.9.9")
    t[0] += login_guard.WINDOW + 1
    assert not login_guard.is_blocked("9.9.9.9")


async def test_roundtrip_still_works():
    assert await decode_string(await encode_string({"chat_id": 1, "msg_id": 2})) == {"chat_id": 1, "msg_id": 2}


async def test_decompression_bomb_is_rejected():
    bomb = base62_encode(zlib.compress(b"0" * (50 * 1024 * 1024), 9))   # ~50 KB compressed, 50 MB expanded
    with pytest.raises(ValueError):
        await decode_string(bomb)
