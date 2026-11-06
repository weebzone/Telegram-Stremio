"""Move the old combined-season pseudo episodes (season 0, episode 1000+N) into episode 1 of season N."""
import copy

_OLD_SEASON = 0
_OLD_EPISODE_BASE = 1000


#----- Return (new_doc, moved_count); the input is not modified and a second run moves nothing
def migrate_show(doc: dict) -> tuple[dict, int]:
    new = copy.deepcopy(doc)
    seasons = new.get("seasons") or []
    old = next((s for s in seasons if s.get("season_number") == _OLD_SEASON), None)
    if not old:
        return doc, 0

    keep, moved = [], 0
    for ep in old.get("episodes") or []:
        number = ep.get("episode_number") or 0
        if number < _OLD_EPISODE_BASE:
            keep.append(ep)          # a real "Specials" episode
            continue
        target_no = number - _OLD_EPISODE_BASE
        season = next((s for s in seasons if s.get("season_number") == target_no), None)
        if season is None:
            season = {"season_number": target_no, "episodes": []}
            seasons.append(season)
        first = next((e for e in season["episodes"] if e.get("episode_number") == 1), None)
        if first is None:
            ep["episode_number"] = 1
            season["episodes"].append(ep)
        else:
            first["telegram"] = (first.get("telegram") or []) + (ep.get("telegram") or [])
        moved += 1

    if not moved:
        return doc, 0
    old["episodes"] = keep
    if not keep:
        seasons.remove(old)
    seasons.sort(key=lambda s: s.get("season_number", 0))
    for s in seasons:
        s["episodes"].sort(key=lambda e: e.get("episode_number", 0))
    return new, moved
