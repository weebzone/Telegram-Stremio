#!/usr/bin/env python3
"""
Telegram-Stremio Video Streaming Diagnostics
Run this to check if media is indexed and streaming is working
"""

import asyncio
import sys
from pathlib import Path

# Add Backend to path
sys.path.insert(0, str(Path(__file__).parent))

async def check_streaming():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║         Telegram-Stremio Streaming Diagnostics                      ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()
    
    try:
        from Backend.helper.database import Database
        from Backend.config import Telegram
        
        print("1️⃣  INITIALIZING DATABASE CONNECTION")
        print("━" * 70)
        
        db = Database()
        await db.connect()
        print("✅ Database connected successfully")
        print()
        
        # Check movies collection
        print("2️⃣  CHECKING MOVIES IN DATABASE")
        print("━" * 70)
        
        movies_col = db.dbs["tracking"]["movies"]
        movie_count = await movies_col.count_documents({})
        print(f"📊 Total movies indexed: {movie_count}")
        
        if movie_count > 0:
            print("\n📝 Sample movies (first 5):")
            async for movie in movies_col.find({}).limit(5):
                title = movie.get("title", "Unknown")
                imdb_id = movie.get("imdb_id", "N/A")
                qualities = movie.get("qualities", {})
                quality_count = sum(len(q.get("parts", [])) for q in qualities.values())
                print(f"   • {title} (IMDB: {imdb_id}) - {quality_count} file(s)")
        else:
            print("⚠️  No movies found in database!")
            print("   → You need to add movies to your Telegram channels/groups")
            print("   → Use the /scan command to index them")
        print()
        
        # Check TV shows collection
        print("3️⃣  CHECKING TV SHOWS IN DATABASE")
        print("━" * 70)
        
        tv_col = db.dbs["tracking"]["tv_shows"]
        tv_count = await tv_col.count_documents({})
        print(f"📊 Total TV shows indexed: {tv_count}")
        
        if tv_count > 0:
            print("\n📝 Sample TV shows (first 5):")
            async for show in tv_col.find({}).limit(5):
                title = show.get("title", "Unknown")
                imdb_id = show.get("imdb_id", "N/A")
                seasons = show.get("seasons", {})
                episode_count = sum(
                    len(season.get("episodes", {})) 
                    for season in seasons.values()
                )
                print(f"   • {title} (IMDB: {imdb_id}) - {episode_count} episode(s)")
        else:
            print("⚠️  No TV shows found in database!")
            print("   → You need to add TV shows to your Telegram channels/groups")
            print("   → Use the /scan command to index them")
        print()
        
        # Check settings
        print("4️⃣  CHECKING BOT SETTINGS")
        print("━" * 70)
        
        settings_col = db.dbs["tracking"]["settings"]
        settings = await settings_col.find_one({"_id": "settings"})
        
        if settings:
            print("✅ Bot settings found:")
            
            # Check stream URL
            stream_url = settings.get("stream_url", "")
            if stream_url:
                print(f"   📡 Stream URL: {stream_url}")
            else:
                print("   ⚠️  Stream URL not configured!")
                print("   → This is required for video playback")
                print("   → Set it in the dashboard: Settings → Stream URL")
            
            # Check bot username
            bot_username = settings.get("bot_username", "")
            if bot_username:
                print(f"   🤖 Bot Username: @{bot_username}")
            
            # Check if global search is enabled
            global_search = settings.get("global_search", False)
            print(f"   🔍 Global Search: {'Enabled' if global_search else 'Disabled'}")
        else:
            print("⚠️  Bot settings not found in database")
        print()
        
        # Check scan status
        print("5️⃣  CHECKING SCAN STATUS")
        print("━" * 70)
        
        scan_col = db.dbs["tracking"]["scan_manager"]
        scan_data = await scan_col.find_one({"_id": "scan_manager"})
        
        if scan_data:
            scanned_messages = scan_data.get("scanned_messages", 0)
            indexed_media = scan_data.get("indexed_media", 0)
            print(f"   📊 Scanned messages: {scanned_messages}")
            print(f"   🎬 Indexed media: {indexed_media}")
            
            if scanned_messages == 0:
                print("\n   ⚠️  No messages have been scanned yet!")
                print("   → Use /scan command in Telegram to start indexing")
        else:
            print("   ⚠️  Scan data not found - no scanning performed yet")
            print("   → Use /scan command in Telegram to start indexing")
        print()
        
        # Summary
        print("╔══════════════════════════════════════════════════════════════════════╗")
        print("║                            SUMMARY                                   ║")
        print("╚══════════════════════════════════════════════════════════════════════╝")
        print()
        
        total_media = movie_count + tv_count
        
        if total_media == 0:
            print("❌ NO MEDIA INDEXED - Video playback won't work!")
            print()
            print("🔧 TO FIX:")
            print("   1. Add your Telegram bot to channels/groups with video files")
            print("   2. Make sure the bot has admin access (or can read messages)")
            print("   3. Send /scan command to the bot in Telegram")
            print("   4. Wait for indexing to complete")
            print("   5. Try playing videos again")
        elif not settings or not settings.get("stream_url"):
            print("⚠️  MEDIA FOUND BUT STREAM URL NOT CONFIGURED!")
            print()
            print("🔧 TO FIX:")
            print("   1. Go to the dashboard: http://your-vps-ip:8081/dashboard")
            print("   2. Navigate to Settings")
            print("   3. Set the Stream URL (usually http://your-vps-ip:8081)")
            print("   4. Save settings")
            print("   5. Try playing videos again")
        else:
            print(f"✅ Setup looks good! {total_media} media file(s) indexed")
            print()
            print("🔍 IF VIDEOS STILL DON'T PLAY:")
            print("   1. Check if the video URL is accessible")
            print("   2. Monitor logs in real-time: tail -f bot_output.log")
            print("   3. Check firewall/port settings on your VPS")
            print("   4. Verify Telegram files haven't expired")
        
    except ImportError as e:
        print(f"❌ Import Error: {e}")
        print("   Make sure you're running this from the project directory")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    print()

if __name__ == "__main__":
    asyncio.run(check_streaming())
