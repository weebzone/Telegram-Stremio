import re

from fastapi.testclient import TestClient

from Backend.fastapi.main import app
from Backend.fastapi.routes import badge_routes


def _client(monkeypatch, tmp_path, fetch=None):
    monkeypatch.setattr(badge_routes, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(badge_routes, "_base_url", lambda request: "https://tv.example.com")
    badge_routes._pack_cache.clear()
    if fetch is not None:
        monkeypatch.setattr(badge_routes, "_download", fetch)
    return TestClient(app)


def test_pack_points_every_badge_at_this_server(monkeypatch, tmp_path):
    r = _client(monkeypatch, tmp_path).get("/badges/prism.json")
    assert r.status_code == 200
    filters = r.json()["filters"]
    assert len(filters) == 261
    assert all(f["imageURL"].startswith("https://tv.example.com/badges/prism/") for f in filters)
    assert all(re.compile(f["pattern"].replace("(?i)", ""), re.I) for f in filters)   # patterns still valid regex


async def _fake_download(name):
    return b"RIFF-webp-" + name.encode()


def test_image_is_downloaded_once_then_served_from_disk(monkeypatch, tmp_path):
    calls = []

    async def fetch(name):
        calls.append(name)
        return await _fake_download(name)

    c = _client(monkeypatch, tmp_path, fetch)
    first = c.get("/badges/prism/4k.webp")
    second = c.get("/badges/prism/4k.webp")
    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "image/webp" and second.content == first.content
    assert calls == ["4k.webp"] and (tmp_path / "4k.webp").exists()
    assert "immutable" in first.headers["cache-control"]


def test_only_images_from_the_pack_can_be_requested(monkeypatch, tmp_path):
    async def fetch(name):
        raise AssertionError("must not fetch " + name)

    c = _client(monkeypatch, tmp_path, fetch)
    assert c.get("/badges/prism/not-a-badge.webp").status_code == 404
    assert c.get("/badges/prism/..%2F..%2Fconfig.env").status_code == 404
    assert c.get("/badges/prism/4k.png").status_code == 404


def test_upstream_failure_is_a_502_and_nothing_is_cached(monkeypatch, tmp_path):
    async def fetch(name):
        raise OSError("network down")

    c = _client(monkeypatch, tmp_path, fetch)
    assert c.get("/badges/prism/4k.webp").status_code == 502
    assert not any(tmp_path.iterdir())


def test_install_page_shows_the_badge_link(monkeypatch):
    import Backend

    async def no_token(_):
        return None
    monkeypatch.setattr(Backend.db, "get_api_token", no_token)
    from starlette.middleware.sessions import SessionMiddleware
    if not any(m.cls is SessionMiddleware for m in app.user_middleware):
        app.add_middleware(SessionMiddleware, secret_key="test")
    r = TestClient(app).get("/stremio/abc/configure")
    assert r.status_code == 200
    assert "/badges/prism.json" in r.text and "Fusion badge URLs" in r.text
