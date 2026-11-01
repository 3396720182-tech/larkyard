#!/bin/sh
set -eu
LABEL="${TICKETD_LABEL:-dev.larkyard.ticketd}"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "uninstalled $LABEL（数据保留在 ${TICKETD_HOME:-$HOME/.ticketd} 和工单目录）"
