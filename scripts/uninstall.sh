#!/bin/sh
set -eu
LABEL="${TICKETD_LABEL:-dev.larkyard.ticketd}"
if [ "$(uname -s)" = Linux ]; then
  systemctl --user disable --now "$LABEL.service" 2>/dev/null || true
  XDG_CFG="$(systemctl --user show-environment 2>/dev/null | sed -n 's/^XDG_CONFIG_HOME=//p')"
  case "$XDG_CFG" in /*) ;; *) XDG_CFG="$HOME/.config";; esac
  rm -f "$XDG_CFG/systemd/user/$LABEL.service"
  systemctl --user daemon-reload 2>/dev/null || true
else
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
fi
echo "uninstalled $LABEL（数据保留在 ${TICKETD_HOME:-$HOME/.ticketd} 和工单目录）"
