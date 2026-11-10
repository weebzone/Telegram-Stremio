import asyncio

import pytest

from Backend.helper import client_health


class FakeSession:
    def __init__(self, ok=True):
        self.ok, self.restarted, self.stopped = ok, 0, 0

    async def send(self, *_a, **_k):
        if not self.ok:
            raise OSError(32, "Broken pipe")
        return True

    async def restart(self):
        self.restarted += 1

    async def stop(self):
        self.stopped += 1


class FakeClient:
    def __init__(self, main_ok=True, media=None):
        self.session = FakeSession(main_ok)
        self.media_sessions = dict(media or {})


async def test_healthy_client_is_left_alone():
    c = FakeClient(media={4: FakeSession()})
    assert await client_health.check_client(1, c) is True
    assert c.session.restarted == 0 and 4 in c.media_sessions


async def test_dead_main_connection_is_restarted():
    c = FakeClient(main_ok=False)
    assert await client_health.check_client(1, c) is False
    assert c.session.restarted == 1


async def test_dead_media_session_is_dropped_so_it_reopens_on_next_play():
    dead = FakeSession(ok=False)
    c = FakeClient(media={2: FakeSession(), 5: dead})
    await client_health.check_client(1, c)
    await asyncio.sleep(0)            # let the background stop() run
    assert 5 not in c.media_sessions and 2 in c.media_sessions and dead.stopped == 1


async def test_concurrent_heals_of_one_client_restart_it_once():
    c = FakeClient(main_ok=False)
    await asyncio.gather(*(client_health.heal(7, c) for _ in range(5)))
    assert c.session.restarted == 1


async def test_pick_working_client_skips_a_broken_one(monkeypatch):
    from Backend.fastapi.routes import stream_routes

    clients = {0: FakeClient(), 1: FakeClient()}
    monkeypatch.setattr(stream_routes, "multi_clients", clients)
    order = iter([0, 1])
    monkeypatch.setattr(stream_routes, "select_best_client", lambda dc, exclude=(): next(order))
    monkeypatch.setattr(stream_routes, "_get_streamer", lambda client, idx: idx)
    healed = []
    monkeypatch.setattr(client_health, "schedule_heal", lambda idx, client: healed.append(idx))

    async def fetch(streamer):
        if streamer == 0:
            raise OSError(32, "Broken pipe")
        return "file-id"

    idx, streamer, result = await stream_routes._db_client(fetch)
    assert (idx, result) == (1, "file-id") and healed == [0]


async def test_pick_working_client_gives_up_after_all_fail(monkeypatch):
    from Backend.fastapi.routes import stream_routes

    clients = {0: FakeClient(), 1: FakeClient()}
    monkeypatch.setattr(stream_routes, "multi_clients", clients)
    order = iter([0, 1, 0])
    monkeypatch.setattr(stream_routes, "select_best_client", lambda dc, exclude=(): next(order))
    monkeypatch.setattr(stream_routes, "_get_streamer", lambda client, idx: idx)
    monkeypatch.setattr(client_health, "schedule_heal", lambda idx, client: None)

    async def fetch(_):
        raise OSError(32, "Broken pipe")

    with pytest.raises(OSError):
        await stream_routes._db_client(fetch)
