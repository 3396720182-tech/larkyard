#!/bin/sh
# 安装 ticketd 守护进程（macOS launchd）。可重复执行：会先卸载同名服务再装。
# 用法：scripts/install.sh [--print]   --print 只把生成的 plist 打到标准输出，不安装、不改任何文件。
# 可选环境变量：TICKETD_LABEL（launchd 标签）、TICKETD_HOME（数据目录）、LARK_CHANNEL_HOME / LARK_CHANNEL_PROFILE（bridge 的 profile）
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="${TICKETD_LABEL:-dev.larkyard.ticketd}"
TICKETD_HOME="${TICKETD_HOME:-$HOME/.ticketd}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LC_HOME="${LARK_CHANNEL_HOME:-$HOME/.lark-channel}"
LC_PROFILE="${LARK_CHANNEL_PROFILE:-claude}"
for t in node lark-cli claude; do command -v "$t" >/dev/null 2>&1 || { echo "install: missing $t on PATH" >&2; exit 1; }; done
NODE_DIR="$(dirname "$(command -v node)")"
LARK_DIR="$(dirname "$(command -v lark-cli)")"
CLAUDE_DIR="$(dirname "$(command -v claude)")"
echo "node=$NODE_DIR lark-cli=$LARK_DIR claude=$CLAUDE_DIR" >&2
P="$REPO/bin:$LARK_DIR:$NODE_DIR:$CLAUDE_DIR:/usr/bin:/bin:/usr/sbin:/sbin"
case "$LABEL" in *[!A-Za-z0-9._-]*|'') echo "install: TICKETD_LABEL 只能含字母、数字和 . _ -" >&2; exit 1;; esac
case "$REPO$HOME$P$LC_HOME$LC_PROFILE$TICKETD_HOME" in *'&'*|*'|'*|*'<'*|*'>'*) echo "install: unsupported character (& | < >) in paths" >&2; exit 1;; esac
render() {
  sed -e "s|@LABEL@|$LABEL|g" -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME|g" -e "s|@PATH@|$P|g" \
      -e "s|@TICKETD_HOME@|$TICKETD_HOME|g" -e "s|@LC_HOME@|$LC_HOME|g" -e "s|@LC_PROFILE@|$LC_PROFILE|g" \
      "$REPO/launchd/ticketd.plist.in"
}
if [ "${1:-}" = "--print" ]; then render; exit 0; fi
mkdir -p "$TICKETD_HOME/logs" "$HOME/Library/LaunchAgents"
[ -f "$TICKETD_HOME/config.env" ] || { cp "$REPO/config.example.env" "$TICKETD_HOME/config.env"; echo "created: $TICKETD_HOME/config.env（先填好 TICKETD_OWNER）"; }
OWNER="$(TICKETD_HOME="$TICKETD_HOME" /usr/bin/python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from ticketd import config; print(config.owner())' "$REPO")"
WORKROOT="$(TICKETD_HOME="$TICKETD_HOME" /usr/bin/python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from ticketd import config; print(config.workroot())' "$REPO")"
[ -n "$OWNER" ] || echo "install: ⚠️ $TICKETD_HOME/config.env 里还没有 TICKETD_OWNER，派单会被拒绝" >&2
mkdir -p "$WORKROOT"
render > "$PLIST"
plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
i=0
while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -gt 50 ] && break
  sleep 1
done
n=0
until launchctl bootstrap "gui/$(id -u)" "$PLIST"; do
  n=$((n + 1))
  if [ "$n" -ge 3 ]; then echo "install: launchctl bootstrap failed after 3 attempts" >&2; exit 1; fi
  sleep 2
done
echo "installed: $PLIST"
echo "workroot: $WORKROOT"
