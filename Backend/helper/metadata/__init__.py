"""
Metadata package.

Priority chains
---------------
Anime  : Kitsu > TVDB > TMDB > Cinemeta   (anime channels only)
Movies : TMDB > Cinemeta
Series : TVDB > Cinemeta > TMDB

Caching
-------
All provider HTTP calls go through ``cached_call`` with in-process dict
caches (search, details, episodes, mappings) plus an ``API_SEMAPHORE``
(max 12 concurrent) so TMDB/TVDB/Kitsu/Cinemeta rate limits are respected.
Identical concurrent lookups share one in-flight Future (no stampedes).
"""
from Backend.helper.metadata.common import (
    caption_with_links,
    extract_default_id,
    format_tmdb_image,
    has_raw_ref,
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
    "analyze_metadata_failure",
    "build_id_link",
    "caption_with_id",
    "caption_with_links",
    "extract_default_id",
    "fetch_movie_metadata",
    "fetch_selected_movie_metadata",
    "fetch_selected_tv_metadata",
    "fetch_tv_metadata",
    "format_tmdb_image",
    "get_tmdb_client",
    "gradient_cover_path",
    "has_raw_ref",
    "metadata",
    "parse_media_name",
    "resolve_cover_url",
    "search_any_candidates",
    "search_movie_candidates",
    "search_tv_candidates",
    "tmdb_api_key",
]
