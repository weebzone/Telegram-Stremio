from datetime import datetime, timedelta

import Backend
from Backend.fastapi.security import tokens
from Backend.helper.bounded import BoundedDict


def test_bounded_dict_evicts_least_recently_used():
    d = BoundedDict(2)
    d["a"], d["b"] = 1, 2
    assert d.get("a") == 1          # touch a -> b is now the oldest
    d["c"] = 3
    assert "b" not in d and d["a"] == 1 and d["c"] == 3


def test_bounded_dict_update_existing_key_keeps_size():
    d = BoundedDict(2)
    d["a"], d["b"] = 1, 2
    d["a"] = 9
    assert len(d) == 2 and d["a"] == 9 and d["b"] == 2


async def test_token_lookup_is_cached_but_callers_get_their_own_copy(monkeypatch):
    tokens.clear_cache()
    calls = []

    async def get_api_token(_):
        calls.append(1)
        return {"token": "t", "user_id": 1, "limits": {}, "usage": {}, "expires_at": datetime.utcnow() + timedelta(days=1)}

    monkeypatch.setattr(Backend.db, "get_api_token", get_api_token)
    a = await tokens.verify_token("t")
    a["limit_exceeded"] = "tampered"
    b = await tokens.verify_token("t")
    assert len(calls) == 1 and b["limit_exceeded"] is None
    tokens.clear_cache()
    await tokens.verify_token("t")
    assert len(calls) == 2
