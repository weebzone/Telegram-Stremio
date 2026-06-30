#!/usr/bin/env python3
"""
Fix Channel Access Issues for Telegram-Stremio
This script will show which channels are configured and their access status
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

async def check_channels():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║         Telegram-Stremio Channel Access Diagnostics                 ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()
    
    try:
        from Backend.helper.database import Database
        from Backend.pyrofork.clients import UserBot
        
        print("1️⃣  INITIALIZING DATABASE AND USERBOT")
        print("━" * 70)
        
        db = Database()
        await db.connect()
        print("✅ Database connected")
        
        # Get settings to find configured channels
        settings_col = db.dbs["tracking"]["settings"]
        settings = await settings_col.find_one({"_id": "settings"})
        
        if not settings:
            print("❌ Settings not found in database")
            return
        
        # Get global search channels
        global_search_channels = settings.get("global_search_channels", [])
        
        print()
        print("2️⃣  CONFIGURED CHANNELS FOR GLOBAL SEARCH")
        print("━" * 70)
        
        if not global_search_channels:
            print("⚠️  No channels configured for global search!")
            print("   This means the bot won't find any videos.")
            print()
            print("🔧 TO FIX:")
            print("   1. Go to dashboard: http://your-vps-ip:8081/dashboard")
            print("   2. Navigate to Settings → Global Search Channels")
            print("   3. Add your Telegram channels/groups that contain videos")
            print()
            return
        
        print(f"📊 Total channels configured: {len(global_search_channels)}")
        print()
        
        # Try to access userbot
        try:
            if not UserBot:
                print("❌ Userbot client not initialized")
                print("   Make sure USER_SESSION_STRING is configured in config.env")
                return
            
            print("3️⃣  CHECKING USERBOT ACCESS TO CHANNELS")
            print("━" * 70)
            print()
            
            accessible = []
            inaccessible = []
            
            for idx, channel_id in enumerate(global_search_channels, 1):
                try:
                    # Try to get chat info
                    chat = await UserBot.get_chat(channel_id)
                    print(f"✅ Channel {idx}: {chat.title or 'Unknown'}")
                    print(f"   ID: {channel_id}")
                    print(f"   Type: {chat.type}")
                    if hasattr(chat, 'username') and chat.username:
                        print(f"   Username: @{chat.username}")
                    accessible.append((channel_id, chat.title or "Unknown"))
                    print()
                except Exception as e:
                    error_msg = str(e)
                    print(f"❌ Channel {idx}: CANNOT ACCESS")
                    print(f"   ID: {channel_id}")
                    print(f"   Error: {error_msg}")
                    inaccessible.append((channel_id, error_msg))
                    print()
            
            # Summary
            print("━" * 70)
            print("📊 SUMMARY")
            print("━" * 70)
            print(f"✅ Accessible channels: {len(accessible)}")
            print(f"❌ Inaccessible channels: {len(inaccessible)}")
            print()
            
            if inaccessible:
                print("🔧 HOW TO FIX INACCESSIBLE CHANNELS:")
                print()
                print("Option 1: Join the channels with your userbot account")
                print(f"   - Log into Telegram with account: @enjeyisback")
                print("   - Join/request access to the inaccessible channels")
                print("   - Restart the bot after joining")
                print()
                print("Option 2: Remove invalid channels from configuration")
                print("   - Go to: http://your-vps-ip:8081/dashboard")
                print("   - Navigate to: Settings → Global Search Channels")
                print("   - Remove the channels you can't access")
                print("   - Keep only channels where userbot is a member")
                print()
                print("Invalid Channel IDs to remove:")
                for channel_id, error in inaccessible:
                    print(f"   - {channel_id}")
            else:
                print("✅ All channels are accessible! Search should work.")
                
        except Exception as e:
            print(f"❌ Error checking userbot access: {e}")
            import traceback
            traceback.print_exc()
            
    except ImportError as e:
        print(f"❌ Import Error: {e}")
        print("   Make sure you're running this from the project directory")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
    
    print()

if __name__ == "__main__":
    asyncio.run(check_channels())
