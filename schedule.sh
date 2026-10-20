#!/bin/bash
# Manage the background schedule (a macOS LaunchAgent for your user only).
#   ./schedule.sh install [minutes]   run every N minutes (default 30)
#   ./schedule.sh uninstall           stop and remove the schedule
#   ./schedule.sh status              show whether it is loaded
#   ./schedule.sh run-now             trigger one run immediately
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="local.junk-cleaner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "${1:-}" in
  install)
    MIN="${2:-30}"
    SECS=$((MIN * 60))
    mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/move-junk-to-deleted.sh</string>
  </array>
  <key>StartInterval</key><integer>$SECS</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.log</string>
</dict>
</plist>
PLIST
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "Scheduled: runs every $MIN minutes (and at login). Log: $DIR/logs/cleaner.log"
    ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Schedule removed."
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      echo "Loaded."; launchctl print "$DOMAIN/$LABEL" | grep -E 'state|last exit code|run interval' || true
    else
      echo "Not scheduled."
    fi
    ;;
  run-now)
    launchctl kickstart "$DOMAIN/$LABEL" && echo "Triggered. Check logs/cleaner.log"
    ;;
  *)
    echo "Usage: $0 install [minutes] | uninstall | status | run-now"; exit 1 ;;
esac
