"""
providers/ — external metadata backends (Cinemeta, TMDB, TVDB, Kitsu).

Each submodule exposes a small, uniform API (search, details, episodes)
used by resolvers.py and entry.py.  HTTP is rate-limited via the shared
API_SEMAPHORE and cached by common.cached_call.
"""

from Backend.helper.metadata.providers import cinemeta, kitsu, tmdb, tvdb

__all__ = ["cinemeta", "kitsu", "tmdb", "tvdb"]
