# Telegram-Stremio Troubleshooting Guide

## 🎬 Video Playback Not Working

If videos aren't playing in Stremio, follow these diagnostic steps:

---

## 📊 Quick Diagnostics (Run on Your VPS)

### Method 1: Run the Error Checker Script
```bash
cd Telegram-Stremio
bash check_errors.sh
```

This script will check:
- ✅ If the bot process is running
- ✅ Recent errors in logs
- ✅ Web server accessibility
- ✅ Database connectivity
- ✅ Stream requests

### Method 2: Run the Streaming Diagnostics
```bash
cd Telegram-Stremio
python3 test_streaming.py
```

This script will check:
- ✅ How many movies/TV shows are indexed
- ✅ If stream URL is configured
- ✅ Bot settings and scan status
- ✅ Database collections

---

## 🔍 Manual Log Monitoring

### View Real-Time Logs
While trying to play a video, run this command to see what's happening:

```bash
tail -f bot_output.log | grep -i --color 'error\|stream\|video\|file'
```

Or to see all activity:
```bash
tail -f bot_output.log
```

### View Recent Errors Only
```bash
tail -100 bot_output.log | grep -i 'error\|exception\|failed'
```

---

## ⚠️ Common Issues & Solutions

### 1. CHANNEL_INVALID Error (Critical - Videos Won't Be Found)
**Symptoms:** 
- Videos don't appear in search results
- Logs show: `[400 CHANNEL_INVALID] The channel parameter is invalid`
- Search completes with 0 results

**Root Cause:** 
The userbot account doesn't have access to the channels configured for global search.

**Solution:**

**Option A: Join the channels with your userbot account**
1. Log into Telegram with your userbot account (`@enjeyisback`)
2. Join/request access to all channels that contain videos
3. Make sure you're accepted (for private channels)
4. Restart the bot: `pkill -f "python.*Backend" && python3 -m Backend`

**Option B: Check which channels are problematic**
```bash
cd Telegram-Stremio
python3 fix_channels.py
```
This script will show:
- Which channels are configured
- Which ones are accessible vs inaccessible
- Exact channel IDs causing problems

**Option C: Remove invalid channels from configuration**
1. Go to: `http://your-vps-ip:8081/dashboard`
2. Navigate to: **Settings → Global Search Channels**
3. Remove channels that the userbot can't access
4. Keep only channels where your userbot is a member
5. Save settings

**Verify the fix:**
- Try searching for a video in Stremio
- Check logs: `tail -f bot_output.log | grep "CHANNEL_INVALID"`
- Should see no more CHANNEL_INVALID errors

---

### 2. No Media Indexed (Most Common)
**Symptoms:** Videos don't appear in Stremio catalog

**Solution:**
1. Add the bot to your Telegram channels/groups with video files
2. Make sure the bot has admin access (or at least can read messages)
3. Send `/scan` command to the bot in Telegram
4. Wait for the scan to complete (check logs for progress)
5. Refresh Stremio catalog

**Verify:**
```bash
python3 test_streaming.py
```

---

### 2. Stream URL Not Configured
**Symptoms:** Videos appear but won't play/stream

**Solution:**
1. Go to: `http://your-vps-ip:8081/dashboard`
2. Navigate to **Settings**
3. Set **Stream URL** to: `http://your-vps-ip:8081`
4. Save settings
5. Try playing videos again

**Check in logs:**
```bash
grep -i "stream_url" bot_output.log
```

---

### 3. Telegram Files Expired
**Symptoms:** Previously working videos suddenly stop working

**Solution:**
1. Re-upload the video files to your Telegram channels
2. Run `/scan` command again to re-index
3. Or manually refresh using bot commands

---

### 4. Port/Firewall Issues
**Symptoms:** Can't access dashboard or videos won't load

**Solution:**
```bash
# Check if port 8081 is open
sudo ufw status
sudo ufw allow 8081

# Or for firewalld
sudo firewall-cmd --add-port=8081/tcp --permanent
sudo firewall-cmd --reload

# Test if port is accessible
curl http://localhost:8081
```

---

### 5. Bot Doesn't Have Channel Access
**Symptoms:** /scan completes but finds no files

**Solution:**
1. Make sure bot is added to your channels/groups
2. Grant bot admin privileges (or at least message read permission)
3. Check bot permissions in channel settings
4. Try /scan again

---

### 6. Database Connection Issues
**Symptoms:** Bot starts but features don't work

**Check logs:**
```bash
tail -100 bot_output.log | grep -i "database\|mongodb"
```

**Solution:**
- Verify MongoDB credentials in `config.env`
- Check if MongoDB Atlas IP whitelist includes your VPS IP
- Test connection manually: `python3 test_streaming.py`

---

## 🔧 Step-by-Step Debug Process

**When a video doesn't play, do this:**

1. **Check if bot is running:**
   ```bash
   ps aux | grep python.*Backend
   ```

2. **Monitor logs in real-time** (in one terminal):
   ```bash
   tail -f bot_output.log
   ```

3. **Try to play the video** (in Stremio)

4. **Watch the logs** - Look for:
   - ❌ Error messages
   - 🔍 Stream requests (URLs being accessed)
   - 📊 Database queries
   - ⚠️ Missing files or expired tokens

5. **Check specific error patterns:**
   ```bash
   # Check for 404 (file not found)
   grep "404" bot_output.log | tail -10
   
   # Check for authentication errors
   grep -i "auth\|unauthorized" bot_output.log | tail -10
   
   # Check for database errors
   grep -i "database.*error" bot_output.log | tail -10
   ```

---

## 📱 Telegram Bot Commands

Use these commands to manage the bot:

- `/start` - Initialize the bot
- `/scan` - Scan channels for media files
- `/stats` - View indexing statistics
- `/settings` - View/modify settings
- `/help` - Get help

---

## 🌐 Dashboard Access

Access the web dashboard at:
```
http://your-vps-ip:8081/dashboard
```

From the dashboard you can:
- View indexed media
- Manage settings
- Configure stream URL
- Monitor scan progress
- Manage custom catalogs

---

## 📋 Logs Location

Default log files:
- `bot_output.log` (if started with nohup)
- `nohup.out` (alternative)
- `log.txt` (if configured)

---

## 🆘 Still Not Working?

If you've tried everything above and videos still don't play:

1. **Share your logs** with the community or support:
   ```bash
   # Get last 50 lines including errors
   tail -50 bot_output.log > debug_info.txt
   ```

2. **Check the GitHub issues**: https://github.com/enjeyisback/Telegram-Stremio/issues

3. **Verify your setup**:
   - Bot is running ✅
   - Database connected ✅
   - Media indexed ✅
   - Stream URL configured ✅
   - Ports accessible ✅

4. **Test with a fresh video**:
   - Upload a new video to Telegram
   - Run /scan
   - Try playing immediately

---

## 💡 Pro Tips

- **Keep logs rotating** to prevent disk space issues
- **Monitor disk space** - video caching can fill up quickly
- **Regular scans** - schedule periodic scans for new content
- **Backup your database** - use MongoDB Atlas backups or manual exports
- **Use multiple bots** - for load balancing with high traffic

---

## 📊 Expected Log Output (Healthy)

When streaming works correctly, you should see:
```
[INFO] - Stream request received for: movie_name
[INFO] - Fetching file from Telegram...
[INFO] - Streaming file: file_id
[200] - GET /stream/xxxxx
```

## ❌ Problem Log Output

If there are issues, you'll see:
```
[ERROR] - File not found in database
[ERROR] - Telegram file expired
[ERROR] - Authentication failed
[404] - Stream URL not found
```

---

**Last Updated:** June 30, 2026
