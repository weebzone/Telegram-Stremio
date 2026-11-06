from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import Backend
from Backend.fastapi.security import tokens


def _token(**kw):
    base = {"token": "t", "user_id": 5, "limits": {}, "usage": {}}
    base.update(kw)
    return base


@pytest.fixture
def serve(monkeypatch):
    def _serve(doc):
        async def get_api_token(_):
            return doc
        monkeypatch.setattr(Backend.db, "get_api_token", get_api_token)
    return _serve


async def test_unknown_token_is_401(serve):
    serve(None)
    with pytest.raises(HTTPException) as e:
        await tokens.verify_token("nope")
    assert e.value.status_code == 401


async def test_valid_token(serve):
    serve(_token(expires_at=datetime.utcnow() + timedelta(days=1)))
    r = await tokens.verify_token("t")
    assert not r["subscription_expired"] and r["limit_exceeded"] is None


async def test_expired_token(serve):
    serve(_token(expires_at=datetime.utcnow() - timedelta(seconds=1)))
    assert (await tokens.verify_token("t"))["subscription_expired"] is True


async def test_admin_never_expires(serve):
    serve(_token(is_admin=True, expires_at=datetime.utcnow() - timedelta(days=9)))
    assert (await tokens.verify_token("t"))["subscription_expired"] is False


async def test_daily_limit(serve):
    gb = 1024 ** 3
    serve(_token(limits={"daily_limit_gb": 2}, usage={"daily": {"bytes": 2 * gb}}))
    assert (await tokens.verify_token("t"))["limit_exceeded"] == "daily"


async def test_under_limit(serve):
    gb = 1024 ** 3
    serve(_token(limits={"daily_limit_gb": 2}, usage={"daily": {"bytes": gb}}))
    assert (await tokens.verify_token("t"))["limit_exceeded"] is None
