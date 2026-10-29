"""
Isolated verification of the LatAddon WebDAV routing logic.
Stubs out heavy dependencies so we can exercise webdav_fs._build_tree
with fake storage data and a fake SettingsManager.
"""
import sys, types, asyncio
from unittest import mock

def _stub(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m

# Backend.db stub (used as `from Backend import db`)
backend = _stub("Backend")
backend.__path__ = []

class FakeCol:
    def __init__(self, docs):
        self.docs = docs
    def find(self, *_a, **_k):
        class _Cursor:
            def __init__(self, docs): self._docs = docs
            async def __aiter__(self):
                for d in self._docs:
                    yield d
        return _Cursor(self.docs)

class FakeDB:
    def __init__(self, movies, tvs):
        self.dbs = {
            "storage_0": {"movie": FakeCol(movies), "tv": FakeCol(tvs)},
        }
        self.current_db_index = 0

fake_db = FakeDB(
    movies=[
        {"tmdb_id": 1, "title": "My Movie", "release_year": 2020,
         "telegram": [{"id": "x1", "name": "My Movie.mkv", "quality": "1080p", "parts": [{"chat_id": "111", "msg_id": 1}]}]},
        {"tmdb_id": 2, "title": "Partner Film", "release_year": 2021,
         "telegram": [{"id": "x2", "name": "Partner Film.mkv", "quality": "1080p", "parts": [{"chat_id": "999", "msg_id": 2}]}]},
    ],
    tvs=[
        {"tmdb_id": 3, "title": "My Show", "release_year": 2019, "telegram": [],
         "seasons": [{"season_number": 1, "episodes": [{"episode_number": 1, "title": "P1", "telegram": [{"id": "e1", "name": "My Show S01E01.mkv", "quality": "1080p", "parts": [{"chat_id": "111", "msg_id": 11}]}]}]}]},
        {"tmdb_id": 4, "title": "Partner Series", "release_year": 2022, "telegram": [],
         "seasons": [{"season_number": 1, "episodes": [{"episode_number": 1, "title": "P1", "telegram": [{"id": "e2", "name": "Partner Series S01E01.mkv", "quality": "1080p", "parts": [{"chat_id": "999", "msg_id": 22}]}]}]}]},
    ],
)
backend.db = fake_db

hh = _stub("Backend.helper")
hh.__path__ = []
sys.modules["Backend.helper"] = hh

sm = _stub("Backend.helper.settings_manager")
class FakeSettings:
    @property
    def lataddon_channels(self):
        return ["999"]
class FakeSM:
    @staticmethod
    def current():
        return FakeSettings()
sm.SettingsManager = FakeSM
sys.modules["Backend.helper.settings_manager"] = sm

nfo = _stub("Backend.helper.nfo_generator")
nfo.movie_nfo = lambda doc: "<movie/>"
nfo.tvshow_nfo = lambda doc: "<tv/>"
nfo.season_nfo = lambda doc, sn: "<season/>"
nfo.episode_nfo = lambda doc, sn, ep: "<ep/>"
sys.modules["Backend.helper.nfo_generator"] = nfo

logger = _stub("Backend.logger")
def _log(*a):
    if len(a) >= 1 and isinstance(a[0], str) and ("%s" in a[0] or "%d" in a[0]) and len(a) > 1:
        try:
            print(a[0] % a[1:]); return
        except Exception:
            pass
    print(*a)
logger.LOGGER = type("L", (), {"info": staticmethod(_log), "warning": staticmethod(_log)})()
sys.modules["Backend.logger"] = logger

pb = _stub("Backend.pyrofork"); pb.__path__ = []
sys.modules["Backend.pyrofork"] = pb
pbb = _stub("Backend.pyrofork.bot")
sys.modules["Backend.pyrofork.bot"] = pbb

class _FakeObjectId:
    pass
bson = _stub("bson")
bson.ObjectId = _FakeObjectId
sys.modules["bson"] = bson

import importlib.util
spec = importlib.util.spec_from_file_location("webdav_fs_test", "Backend/helper/webdav_fs.py")
wfs = importlib.util.module_from_spec(spec)
sys.modules["webdav_fs_test"] = wfs
spec.loader.exec_module(wfs)

async def main():
    fs = wfs.WebDAVFilesystem(cache_ttl=0)
    root = await fs._build_tree()

    la = root.children.get("LatAddon")
    assert la is not None, "LatAddon folder missing"
    la_movies = la.children["Movies"]
    la_tv = la.children["TV Shows"]
    root_movies = root.children["Movies"]
    root_tv = root.children["TV Shows"]

    # Partner content must be in /LatAddon and NOT in root
    assert any("Partner Film" in c for c in la_movies.children), "Partner Film not in /LatAddon/Movies"
    assert all("Partner" not in c for c in root_movies.children), "Partner content leaked into root /Movies"
    assert any("Partner Series" in c for c in la_tv.children), "Partner Series not in /LatAddon/TV Shows"
    assert all("Partner" not in c for c in root_tv.children), "Partner series leaked into root /TV Shows"
    # Normal content stays in root
    assert any("My Movie" in c for c in root_movies.children), "My Movie missing from root /Movies"
    assert any("My Show" in c for c in root_tv.children), "My Show missing from root /TV Shows"
    print("ALL LATADDON ROUTING ASSERTIONS PASSED ✅")

asyncio.run(main())
