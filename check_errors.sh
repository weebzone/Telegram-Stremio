#!/bin/bash
# Telegram-Stremio Error Checker Script
# Run this on your VPS to diagnose video playback issues

echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║         Telegram-Stremio Video Playback Diagnostics                 ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""

# 1. Check if bot is running
echo "1️⃣  CHECKING BOT PROCESS STATUS"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if pgrep -f "python.*Backend" > /dev/null; then
    echo "✅ Bot process is running"
    echo "   PID: $(pgrep -f "python.*Backend")"
else
    echo "❌ Bot process is NOT running!"
    echo "   Start the bot first: python3 -m Backend"
    exit 1
fi
echo ""

# 2. Check recent errors in logs
echo "2️⃣  CHECKING FOR ERRORS IN LOGS (Last 100 lines)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
# Try to find the log file
LOG_FILE=""
if [ -f "bot_output.log" ]; then
    LOG_FILE="bot_output.log"
elif [ -f "nohup.out" ]; then
    LOG_FILE="nohup.out"
elif [ -f "log.txt" ]; then
    LOG_FILE="log.txt"
else
    echo "⚠️  Could not find log file. Checking stderr/stdout..."
fi

if [ -n "$LOG_FILE" ]; then
    ERROR_COUNT=$(tail -100 "$LOG_FILE" | grep -i "error\|exception\|failed\|traceback" | wc -l)
    if [ "$ERROR_COUNT" -gt 0 ]; then
        echo "⚠️  Found $ERROR_COUNT error entries in last 100 lines:"
        echo ""
        tail -100 "$LOG_FILE" | grep -i -A 3 "error\|exception\|failed" | tail -30
    else
        echo "✅ No errors found in recent logs"
    fi
else
    echo "⚠️  No log file found"
fi
echo ""

# 3. Check web server accessibility
echo "3️⃣  CHECKING WEB SERVER ACCESSIBILITY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
PORT=$(grep -oP 'PORT="\K[^"]+' config.env 2>/dev/null || echo "8081")
if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT" | grep -q "302\|200"; then
    echo "✅ Web server is responding on port $PORT"
else
    echo "❌ Web server is NOT responding on port $PORT"
    echo "   Check if the port is blocked or the server failed to start"
fi
echo ""

# 4. Check if there are any video requests being logged
echo "4️⃣  CHECKING FOR VIDEO STREAM REQUESTS (Last 50 lines)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ -n "$LOG_FILE" ]; then
    STREAM_COUNT=$(tail -50 "$LOG_FILE" | grep -i "stream\|video\|/file/\|download" | wc -l)
    if [ "$STREAM_COUNT" -gt 0 ]; then
        echo "✅ Found $STREAM_COUNT stream-related log entries:"
        echo ""
        tail -50 "$LOG_FILE" | grep -i "stream\|video\|/file/\|download" | tail -10
    else
        echo "⚠️  No video stream requests found in recent logs"
        echo "   This might indicate the video URLs aren't being accessed"
    fi
fi
echo ""

# 5. Check database connectivity
echo "5️⃣  CHECKING DATABASE CONNECTIVITY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ -n "$LOG_FILE" ]; then
    DB_ERRORS=$(tail -100 "$LOG_FILE" | grep -i "database.*error\|mongodb.*failed\|bad auth" | wc -l)
    if [ "$DB_ERRORS" -gt 0 ]; then
        echo "❌ Found database connectivity issues:"
        tail -100 "$LOG_FILE" | grep -i "database.*error\|mongodb.*failed\|bad auth" | tail -5
    else
        echo "✅ No database errors found"
    fi
fi
echo ""

# 6. Check if media is in database
echo "6️⃣  CHECKING IF MEDIA IS INDEXED IN DATABASE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "⚠️  To check if videos are indexed, you need to:"
echo "   1. Add media to your Telegram channels/groups"
echo "   2. Use the bot's /scan command to index them"
echo "   3. Check the dashboard at http://your-vps-ip:$PORT/dashboard"
echo ""

# 7. Real-time log monitoring suggestion
echo "7️⃣  REAL-TIME ERROR MONITORING"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 To monitor errors in real-time while trying to play a video:"
echo ""
if [ -n "$LOG_FILE" ]; then
    echo "   tail -f $LOG_FILE | grep -i --color 'error\|exception\|failed\|stream\|video'"
else
    echo "   tail -f bot_output.log | grep -i --color 'error\|exception\|failed\|stream\|video'"
fi
echo ""
echo "   OR to see all activity:"
if [ -n "$LOG_FILE" ]; then
    echo "   tail -f $LOG_FILE"
else
    echo "   tail -f bot_output.log"
fi
echo ""

echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║                     Common Video Playback Issues                     ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""
echo "🔍 COMMON CAUSES:"
echo "   1. ❌ No media indexed - Run the /scan command in Telegram"
echo "   2. ❌ Wrong stream URL - Check if the URL format is correct"
echo "   3. ❌ Telegram file expired - Re-upload or refresh the file"
echo "   4. ❌ Network/firewall blocking - Check VPS firewall rules"
echo "   5. ❌ Port not exposed - Ensure port $PORT is accessible"
echo "   6. ❌ Missing channel permissions - Bot needs admin access"
echo ""
