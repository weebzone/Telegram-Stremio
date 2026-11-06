"""One-off: move old combined-season entries (season 0, episode 1000+N) into episode 1 of season N.

Dry run by default. Back up your database first (Settings > Backup), then:
    uv run python scripts/migrate_combined.py            # shows what would change
    uv run python scripts/migrate_combined.py --apply    # writes the changes
Safe to run twice: already-migrated shows are left alone.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, so `Backend` imports

from Backend import db
from Backend.helper.combined_migration import migrate_show


async def main():
    apply = "--apply" in sys.argv
    await db.connect()
    shows = entries = 0
    for key, database in db.dbs.items():
        if not key.startswith("storage_"):
            continue
        query = {"seasons": {"$elemMatch": {"season_number": 0, "episodes.episode_number": {"$gte": 1000}}}}
        async for doc in database["tv"].find(query):
            new, moved = migrate_show(doc)
            if not moved:
                continue
            shows += 1
            entries += moved
            print(f"{'MOVE' if apply else 'would move'} {moved} combined entr{'y' if moved == 1 else 'ies'}: {doc.get('title')} [{key}]")
            if apply:
                await database["tv"].replace_one({"_id": doc["_id"]}, new)
    print(f"\n{shows} show(s), {entries} combined entr{'y' if entries == 1 else 'ies'} {'migrated' if apply else 'to migrate (dry run, nothing written)'}.")


asyncio.run(main())
