#!/bin/bash
# Installs the monitor as a macOS background job (launchd) that runs every
# CHECK_INTERVAL seconds, also after reboot. Re-run after editing .env.
#
# Files are copied to ~/Library/Application Support because macOS blocks
# background jobs from reading ~/Desktop.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/chrome-hearts-monitor"
LABEL="com.chromehearts-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY=/usr/bin/python3

if [ ! -f "$SRC/.env" ]; then
    echo "No .env found. Run: cp .env.example .env  — then fill it in."
    exit 1
fi

INTERVAL=$(grep -E '^CHECK_INTERVAL=' "$SRC/.env" | cut -d= -f2 | tr -d ' "' || true)
INTERVAL=${INTERVAL:-120}

mkdir -p "$DEST" "$HOME/Library/LaunchAgents"
cp "$SRC/monitor.py" "$DEST/monitor.py"
cp "$SRC/.env" "$DEST/.env"
chmod 600 "$DEST/.env"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$DEST/monitor.py</string>
    </array>
    <key>WorkingDirectory</key><string>$DEST</string>
    <key>StartInterval</key><integer>$INTERVAL</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$DEST/monitor.log</string>
    <key>StandardErrorPath</key><string>$DEST/monitor.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "Installed. Checking every $INTERVAL seconds."
echo "Log:  tail -f \"$DEST/monitor.log\""
