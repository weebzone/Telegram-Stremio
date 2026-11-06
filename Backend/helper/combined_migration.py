"""Move the old combined-season pseudo episodes (season 0, episode 1000+N) to where new uploads go:
season N, episode = first episode of the range in the file name (episode 1 for full-season packs)."""
import copy

from Backend.helper.split_files import parse_combined_episodes

_OLD_SEASON = 0
_OLD_EPISODE_BASE = 1000


def _target_episode(quality: dict) -> int:
    combined = parse_combined_episodes(quality.get("name") or "")
    return (combined or {}).get("start") or 1


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
        season_no = number - _OLD_EPISODE_BASE
        season = next((s for s in seasons if s.get("season_number") == season_no), None)
        if season is None:
            season = {"season_number": season_no, "episodes": []}
            seasons.append(season)
        for quality in ep.get("telegram") or []:
            ep_no = _target_episode(quality)
            target = next((e for e in season["episodes"] if e.get("episode_number") == ep_no), None)
            if target is None:
                target = {k: v for k, v in ep.items() if k != "telegram"}
                target.update(episode_number=ep_no, title=f"Episode {ep_no}", telegram=[])
                season["episodes"].append(target)
            target["telegram"] = (target.get("telegram") or []) + [quality]
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
