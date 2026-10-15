"""Episode coverage: compare uploaded episodes (DB) vs full season list (Cinemeta).

Cinemeta needs no API key (the repo already uses it as a fallback), so this works
even without a TMDB key configured. Results are cached on the media doc under
`coverage_map` to avoid hitting Cinemeta on every view.
"""
from typing import Dict, List, Optional

from Backend.helper.metadata.providers.cinemeta import get_detail
from Backend.logger import LOGGER


async def _cinemeta_season_episodes(imdb_id: str) -> Dict[int, List[int]]:
    """Return {season_number: sorted list of episode numbers} from Cinemeta.

    Specials (season 0) are excluded. Returns {} on any failure.
    """
    out: Dict[int, List[int]] = {}
    try:
        detail = await get_detail(imdb_id=imdb_id, media_type="series")
        videos = (detail or {}).get("videos") or []
        for v in videos:
            try:
                s = int(v.get("season") or 0)
                e = int(v.get("episode") or 0)
            except (TypeError, ValueError):
                continue
            if s <= 0 or e <= 0:
                continue
            out.setdefault(s, []).append(e)
        for s in out:
            out[s] = sorted(set(out[s]))
    except Exception as e:
        LOGGER.warning(f"[COVERAGE] Cinemeta lookup failed for {imdb_id}: {e}")
    return out


def _db_uploaded_episodes(doc: dict) -> Dict[int, set]:
    """Return {season_number: set(episode_numbers)} that have a telegram upload."""
    out: Dict[int, set] = {}
    for season in (doc.get("seasons") or []):
        try:
            s = int(season.get("season_number") or 0)
        except (TypeError, ValueError):
            continue
        if s <= 0:
            continue
        eps = set()
        for ep in (season.get("episodes") or []):
            if not ep.get("telegram"):
                continue
            try:
                e = int(ep.get("episode_number") or 0)
            except (TypeError, ValueError):
                continue
            if e > 0:
                eps.add(e)
        if eps:
            out[s] = eps
    return out


async def compute_coverage(doc: dict, *, refresh: bool = False) -> dict:
    """Compute episode coverage for a TV doc.

    Returns:
        {
            "coverage_pct": float,
            "total_expected": int,
            "total_have": int,
            "seasons": [{"n": int, "total": int, "have": int, "missing": [int, ...]}],
        }
    Cached in doc["coverage_map"] unless refresh=True.
    """
    imdb_id = doc.get("imdb_id") or ""
    if not imdb_id:
        return {"coverage_pct": 0.0, "total_expected": 0, "total_have": 0, "seasons": []}

    # Serve from cache unless a refresh was requested.
    cached = doc.get("coverage_map") or {}
    if not refresh and cached.get("imdb_id") == imdb_id and cached.get("seasons"):
        return {k: v for k, v in cached.items() if k != "imdb_id"}

    full = await _cinemeta_season_episodes(imdb_id)
    have = _db_uploaded_episodes(doc)

    seasons_out: List[dict] = []
    total_expected = 0
    total_have = 0
    for s in sorted(full.keys()):
        expected = full[s]
        got = have.get(s, set())
        have_count = sum(1 for e in expected if e in got)
        missing = [e for e in expected if e not in got]
        total_expected += len(expected)
        total_have += have_count
        seasons_out.append({
            "n": s,
            "total": len(expected),
            "have": have_count,
            "missing": missing,
        })

    pct = round(100.0 * total_have / total_expected, 1) if total_expected else 0.0
    result = {
        "coverage_pct": pct,
        "total_expected": total_expected,
        "total_have": total_have,
        "seasons": seasons_out,
    }

    # Persist cache onto the doc for next time.
    try:
        from Backend import db
        await db.dbs[f"storage_{int(doc.get('db_index', 1))}"]["tv"].update_one(
            {"_id": doc.get("_id")},
            {"$set": {"coverage_map": {**result, "imdb_id": imdb_id}}},
        )
    except Exception as e:
        LOGGER.warning(f"[COVERAGE] cache write failed for {imdb_id}: {e}")
    return result
