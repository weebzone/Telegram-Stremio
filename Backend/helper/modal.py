from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


#----- Quality detail schema
class QualityPart(BaseModel):
    part_number: int
    chat_id: int
    msg_id: int
    size_bytes: int


class QualityDetail(BaseModel):
    quality: str
    id: str
    name: str
    size: str
    group_key: Optional[str] = None
    parts: Optional[List[QualityPart]] = None


#----- Episode schema
class Episode(BaseModel):
    episode_number: int
    title: str
    episode_backdrop: Optional[str] = None
    overview: Optional[str] = None
    released: Optional[str] = None
    telegram: Optional[List[QualityDetail]]


#----- Season schema
class Season(BaseModel):
    season_number: int
    episodes: List[Episode] = Field(default_factory=list)


#----- TV show schema
class TVShowSchema(BaseModel):
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    db_index: int
    title: str
    genres: Optional[List[str]] = None
    description: Optional[str] = None
    rating: Optional[float] = None
    release_year: Optional[int] = None
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    logo: Optional[str] = None
    cast: Optional[List[str]] = None
    runtime: Optional[str] = None
    media_type: str
    updated_on: datetime = Field(default_factory=datetime.utcnow)
    seasons: List[Season] = Field(default_factory=list)
    is_anime: Optional[bool] = False
    original_language: Optional[str] = None
    origin_country: Optional[List[str]] = Field(default_factory=list)
    production_countries: Optional[List[str]] = Field(default_factory=list)
    watch_providers: Optional[List[str]] = Field(default_factory=list)
    auto_tags: Optional[List[str]] = Field(default_factory=list)
    auto_catalog: Optional[dict] = None


#----- Movie schema
class MovieSchema(BaseModel):
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    db_index: int
    title: str
    genres: Optional[List[str]] = None
    description: Optional[str] = None
    rating: Optional[float] = None
    release_year: Optional[int] = None
    poster: Optional[str] = None
    backdrop: Optional[str] = None
    logo: Optional[str] = None
    cast: Optional[List[str]] = None
    runtime: Optional[str] = None
    media_type: str
    updated_on: datetime = Field(default_factory=datetime.utcnow)
    telegram: Optional[List[QualityDetail]]
    is_anime: Optional[bool] = False
    original_language: Optional[str] = None
    origin_country: Optional[List[str]] = Field(default_factory=list)
    production_countries: Optional[List[str]] = Field(default_factory=list)
    watch_providers: Optional[List[str]] = Field(default_factory=list)
    auto_tags: Optional[List[str]] = Field(default_factory=list)
    auto_catalog: Optional[dict] = None
