"""Incomplete-series view: TV shows with missing seasons/episodes.

Reuses helper.tv_coverage, which compares the uploaded episodes in the DB with
the full season list from Cinemeta and caches the result on the media document.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

from Backend import db
from Backend.helper.tv_coverage import compute_coverage
from Backend.logger import LOGGER

router = APIRouter(prefix="/api/media", tags=["Incomplete"])

_PROJECTION = {
    "_id": 1,
    "title": 1,
    "title_english": 1,
    "original_title": 1,
    "poster": 1,
    "imdb_id": 1,
    "tmdb_id": 1,
    "db_index": 1,
    "seasons": 1,
    "coverage_map": 1,
}

_CONCURRENCY = 8


async def _load_docs(search: str) -> List[dict]:
    query: Dict[str, Any] = {"imdb_id": {"$exists": True, "$nin": [None, ""]}}
    if search:
        rx = {"$regex": re.escape(search), "$options": "i"}
        query["$or"] = [
            {"title": rx},
            {"title_english": rx},
            {"original_title": rx},
        ]

    docs: List[dict] = []
    for index in range(1, db.current_db_index + 1):
        database = db.dbs.get(f"storage_{index}")
        if database is None:
            continue
        async for doc in database["tv"].find(query, _PROJECTION):
            doc["db_index"] = doc.get("db_index") or index
            docs.append(doc)
    return docs


async def _coverage_item(doc: dict, refresh: bool, sem: asyncio.Semaphore) -> Optional[dict]:
    async with sem:
        try:
            coverage = await compute_coverage(doc, refresh=refresh)
        except Exception as e:
            LOGGER.warning(f"[INCOMPLETE] coverage failed for {doc.get('imdb_id')}: {e}")
            return None

    if not coverage.get("total_expected"):
        return None
    if coverage.get("coverage_pct", 100.0) >= 100.0:
        return None

    missing = [
        {"season": season.get("n"), "episodes": season.get("missing") or []}
        for season in coverage.get("seasons", [])
        if season.get("missing")
    ]
    if not missing:
        return None

    return {
        "tmdb_id": doc.get("tmdb_id"),
        "db_index": doc.get("db_index", 1),
        "title": doc.get("title") or "",
        "poster": doc.get("poster") or "",
        "imdb_id": doc.get("imdb_id") or "",
        "coverage_pct": coverage.get("coverage_pct", 0.0),
        "total_expected": coverage.get("total_expected", 0),
        "total_have": coverage.get("total_have", 0),
        "missing_count": sum(len(m["episodes"]) for m in missing),
        "missing": missing,
    }


@router.get("/incomplete")
async def list_incomplete(
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    search: str = Query(""),
    refresh: bool = Query(False),
):
    docs = await _load_docs(search.strip())
    sem = asyncio.Semaphore(_CONCURRENCY)
    results = await asyncio.gather(*(_coverage_item(doc, refresh, sem) for doc in docs))
    items = [item for item in results if item]

    items.sort(key=lambda x: (x["coverage_pct"], -x["missing_count"], (x["title"] or "").lower()))
    total = len(items)
    start = (page - 1) * page_size

    return {
        "items": items[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
