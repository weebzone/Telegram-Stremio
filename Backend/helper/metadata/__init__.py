"""
Metadata package — public API for media identification and enrichment.

Re-exports the main entry points used by scanners, receivers, Stremio routes
and the admin UI:

  - metadata() / parse_media_name()          — turn a filename into structured data
  - resolve_* / fetch_selected_*             — look up TMDB / TVDB / Kitsu / Cinemeta
  - extract_default_id, caption_with_id      — ID helpers for captions & links
  - resolve_cover_url, format_tmdb_image     — artwork helpers
  - COMBINED_SEASON / COMBINED_EPISODE_BASE  — constants for multi-episode files

Example
-------
    from Backend.helper.metadata import metadata, parse_media_name

    info = parse_media_name("Avatar.2009.1080p.BluRay.x264.mkv")
    # -> media_type, title, year, quality, ...

    result = await metadata(chat_id, message, filename)
    # -> full DB-ready document with ids, poster, seasons, etc.
"""

from Backend.helper.metadata.common import (
    COMBINED_EPISODE_BASE,
    COMBINED_SEASON,
    extract_default_id,
    format_tmdb_image,
    gradient_cover_path,
    resolve_cover_url,
)
from Backend.helper.metadata.entry import (
    analyze_metadata_failure,
    build_id_link,
    caption_with_id,
    fetch_selected_movie_metadata,
    fetch_selected_tv_metadata,
    metadata,
    search_any_candidates,
    search_movie_candidates,
    search_tv_candidates,
)
from Backend.helper.metadata.parse import parse_media_name
from Backend.helper.metadata.providers.tmdb import get_tmdb_client, tmdb_api_key
from Backend.helper.metadata.resolvers import (
    resolve_movie as fetch_movie_metadata,
    resolve_series as fetch_tv_metadata,
)

__all__ = [
    "COMBINED_EPISODE_BASE",
    "COMBINED_SEASON",
    "analyze_metadata_failure",
    "build_id_link",
    "caption_with_id",
    "extract_default_id",
    "fetch_movie_metadata",
    "fetch_selected_movie_metadata",
    "fetch_selected_tv_metadata",
    "fetch_tv_metadata",
    "format_tmdb_image",
    "get_tmdb_client",
    "gradient_cover_path",
    "metadata",
    "parse_media_name",
    "resolve_cover_url",
    "search_any_candidates",
    "search_movie_candidates",
    "search_tv_candidates",
    "tmdb_api_key",
]
