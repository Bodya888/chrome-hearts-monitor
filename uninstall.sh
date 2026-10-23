#!/bin/bash
# Stops and removes the background job. Keeps the copied files and log.
PLIST="$HOME/Library/LaunchAgents/com.chromehearts-monitor.plist"
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Stopped. Files remain in: $HOME/Library/Application Support/chrome-hearts-monitor"
