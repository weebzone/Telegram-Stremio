from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import Backend
from Backend.fastapi.main import app
from Backend.fastapi.security.credentials import require_auth

app.add_middleware(SessionMiddleware, secret_key="test")  # normally added in __main__ at startup


def _client():
    app.dependency_overrides[require_auth] = lambda: True
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def test_page_renders():
    r = _client().get("/admin/skipped")
    assert r.status_code == 200 and "Skipped files" in r.text


def test_list_uses_db(monkeypatch):
    async def fake(status=None, page=1, per_page=50):
        return {"total": 1, "items": [{"_id": "1:2", "chat_id": "1", "msg_id": 2, "file_name": "a.mkv",
                                        "raw_size": 10, "reason": "No title", "status": status or "pending"}]}
    monkeypatch.setattr(Backend.db, "list_skipped_files", fake)
    body = _client().get("/api/admin/skipped?status=pending").json()
    assert body["total"] == 1 and body["items"][0]["id"] == "1:2" and body["items"][0]["reason"] == "No title"


def test_stats_requires_auth():
    r = TestClient(app, follow_redirects=False).get("/stream/stats")
    assert r.status_code == 302 and r.headers["location"] == "/login"   # no stats without an admin session


def test_login_throttle_returns_429():
    from Backend.helper import login_guard
    login_guard.reset()
    c = TestClient(app, follow_redirects=False)
    codes = [c.post("/login", data={"username": "x", "password": "y"}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    login_guard.reset()
