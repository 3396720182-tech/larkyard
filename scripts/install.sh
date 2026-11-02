#!/bin/sh
# 安装 ticketd 守护进程：macOS 用 launchd，Linux（含 WSL2）用 systemd 用户服务。可重复执行：会先停掉同名服务再装。
# 用法：scripts/install.sh [--print]   --print 只把生成的 plist / unit 打到标准输出，不安装、不改任何文件。
# 可选环境变量：TICKETD_LABEL（服务名）、TICKETD_HOME（数据目录）、LARK_CHANNEL_HOME / LARK_CHANNEL_PROFILE（bridge 的 profile）、
#   TICKETD_PYTHON（Python 3.9+ 解释器，默认 /usr/bin/python3；Linux 上会写进服务环境）
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OS="$(uname -s)"
case "$OS" in Darwin|Linux) ;; *) echo "install: 不支持 $OS（只支持 macOS 和 Linux / WSL2）" >&2; exit 1;; esac
LABEL="${TICKETD_LABEL:-dev.larkyard.ticketd}"
TICKETD_HOME="${TICKETD_HOME:-$HOME/.ticketd}"
LC_HOME="${LARK_CHANNEL_HOME:-$HOME/.lark-channel}"
LC_PROFILE="${LARK_CHANNEL_PROFILE:-claude}"
PY="${TICKETD_PYTHON:-/usr/bin/python3}"
if [ -z "${TICKETD_PYTHON:-}" ] && [ ! -x "$PY" ]; then PY="$(command -v python3 || echo /usr/bin/python3)"; fi
for t in node lark-cli claude; do command -v "$t" >/dev/null 2>&1 || { echo "install: missing $t on PATH" >&2; exit 1; }; done
NODE_DIR="$(dirname "$(command -v node)")"
LARK_DIR="$(dirname "$(command -v lark-cli)")"
CLAUDE_DIR="$(dirname "$(command -v claude)")"
CODEX_DIR=""  # 可选：装了 codex 就把它所在目录也放进 PATH（Codex 执行器用）
if command -v codex >/dev/null 2>&1; then CODEX_DIR="$(dirname "$(command -v codex)"):"; fi
echo "node=$NODE_DIR lark-cli=$LARK_DIR claude=$CLAUDE_DIR codex=${CODEX_DIR%:}" >&2
if [ "$OS" = Darwin ]; then
  P="$REPO/bin:$LARK_DIR:$NODE_DIR:$CLAUDE_DIR:$CODEX_DIR/usr/bin:/bin:/usr/sbin:/sbin"
  TEMPLATE="$REPO/launchd/ticketd.plist.in"
  SERVICE_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"
else
  # bwrap、socat（沙箱要用）在 /usr/bin；/usr/local/bin 放手动装的工具
  P="$REPO/bin:$LARK_DIR:$NODE_DIR:$CLAUDE_DIR:$CODEX_DIR/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
  TEMPLATE="$REPO/systemd/ticketd.service.in"
  SERVICE_FILE=""  # 要问过 systemd 用户实例才知道放哪（见下面）
fi
case "$LABEL" in *[!A-Za-z0-9._-]*|'') echo "install: TICKETD_LABEL 只能含字母、数字和 . _ -" >&2; exit 1;; esac
case "$REPO$HOME$P$LC_HOME$LC_PROFILE$TICKETD_HOME$PY" in *'&'*|*'|'*|*'<'*|*'>'*) echo "install: unsupported character (& | < >) in paths" >&2; exit 1;; esac
if [ "$OS" = Linux ]; then
  # systemd 的 unit 文件里 % 是占位符、$ 会被展开、空白会拆开 append: 路径，不值得逐个转义
  case "$REPO$HOME$P$LC_HOME$LC_PROFILE$TICKETD_HOME$PY" in
    *[[:space:]]*|*'%'*|*'$'*|*'"'*|*'\'*) echo "install: Linux 上路径里不能有空白或 % \$ \" \\ 字符" >&2; exit 1;;
  esac
  case "$(uname -r)" in
    *Microsoft*) echo "install: 这是 WSL1，不支持：Claude Code 的沙箱只支持 WSL2，WSL1 也没有 systemd（PowerShell：wsl --set-version <发行版> 2）" >&2; exit 1;;
    *microsoft*)
      # WSL2 里 PATH 常带着 Windows 的目录：claude / node / lark-cli 解析到 Windows 版就不在 Linux 沙箱里跑了
      for d in "$NODE_DIR" "$LARK_DIR" "$CLAUDE_DIR" "${CODEX_DIR%:}"; do
        case "$d" in /mnt/[a-z]/*) echo "install: $d 在 Windows 盘上：node、lark-cli、claude、codex 要装 WSL 里的 Linux 版（可以用 nvm）" >&2; exit 1;; esac
      done;;
  esac
fi
render() {
  sed -e "s|@LABEL@|$LABEL|g" -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME|g" -e "s|@PATH@|$P|g" \
      -e "s|@TICKETD_HOME@|$TICKETD_HOME|g" -e "s|@LC_HOME@|$LC_HOME|g" -e "s|@LC_PROFILE@|$LC_PROFILE|g" \
      -e "s|@PYTHON@|$PY|g" \
      "$TEMPLATE" | if [ "$OS" = Darwin ] && [ -n "${TICKETD_PYTHON:-}" ]; then
    # 显式指定了解释器才写进 plist，没指定时 plist 和以前一模一样
    sed -e "s|^\(    <key>TICKETD_HOME</key>.*\)$|\1\\
    <key>TICKETD_PYTHON</key><string>$PY</string>|"
  else
    cat
  fi
}
if [ "${1:-}" = "--print" ]; then render; exit 0; fi
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null \
  || { echo "install: 需要 Python 3.9+（当前：$PY）；用 TICKETD_PYTHON 指定解释器" >&2; exit 1; }
if [ "$OS" = Linux ]; then
  if ! command -v systemctl >/dev/null 2>&1 || ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "install: 没有可用的 systemd 用户实例（systemctl --user 连不上）。" >&2
    echo "  WSL2：在 /etc/wsl.conf 里写 [boot] 和 systemd=true 两行，在 PowerShell 里执行 wsl --shutdown，再重新打开发行版。" >&2
    echo "  不用 systemd：scripts/install.sh --print 可以看生成的 unit，自行用别的方式常驻运行 bin/ticketd。" >&2
    exit 1
  fi
  # unit 要放在 systemd 用户实例自己的配置目录里：它看的是自己环境里的 XDG_CONFIG_HOME，不是当前 shell 的
  XDG_CFG="$(systemctl --user show-environment 2>/dev/null | sed -n 's/^XDG_CONFIG_HOME=//p')"
  case "$XDG_CFG" in /*) ;; *) XDG_CFG="$HOME/.config";; esac
  case "$XDG_CFG" in *[[:space:]]*|*'&'*|*'|'*) echo "install: systemd 的 XDG_CONFIG_HOME 路径不支持：$XDG_CFG" >&2; exit 1;; esac
  SERVICE_FILE="$XDG_CFG/systemd/user/$LABEL.service"
  for t in bwrap socat; do
    command -v "$t" >/dev/null 2>&1 || echo "install: ⚠️ PATH 上没有 $t：worker 的沙箱要用它，没有时 worker 会起不来（Ubuntu/Debian：sudo apt-get install bubblewrap socat）" >&2
  done
  if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ] \
     && [ ! -e /etc/apparmor.d/bwrap ] && [ ! -e /etc/apparmor.d/bwrap-userns-restrict ]; then
    echo "install: ⚠️ AppArmor 限制了非特权用户命名空间（Ubuntu 24.04 起的默认值），bwrap 可能起不来；按 README「Linux / WSL2」一节给 bwrap 加 AppArmor 配置" >&2
  fi
fi
mkdir -p "$TICKETD_HOME/logs" "$(dirname "$SERVICE_FILE")"
[ -f "$TICKETD_HOME/config.env" ] || { cp "$REPO/config.example.env" "$TICKETD_HOME/config.env"; echo "created: $TICKETD_HOME/config.env（先填好 TICKETD_OWNER）"; }
OWNER="$(TICKETD_HOME="$TICKETD_HOME" "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from ticketd import config; print(config.owner())' "$REPO")"
WORKROOT="$(TICKETD_HOME="$TICKETD_HOME" "$PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from ticketd import config; print(config.workroot())' "$REPO")"
[ -n "$OWNER" ] || echo "install: ⚠️ $TICKETD_HOME/config.env 里还没有 TICKETD_OWNER，派单会被拒绝" >&2
mkdir -p "$WORKROOT"
render > "$SERVICE_FILE"
if [ "$OS" = Darwin ]; then
  plutil -lint "$SERVICE_FILE"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  i=0
  while launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; do
    i=$((i + 1))
    [ "$i" -gt 50 ] && break
    sleep 1
  done
  n=0
  until launchctl bootstrap "gui/$(id -u)" "$SERVICE_FILE"; do
    n=$((n + 1))
    if [ "$n" -ge 3 ]; then echo "install: launchctl bootstrap failed after 3 attempts" >&2; exit 1; fi
    sleep 2
  done
else
  systemctl --user daemon-reload
  systemctl --user enable "$LABEL.service" >/dev/null
  systemctl --user restart "$LABEL.service"  # 没在跑就启动，在跑就重启（和 macOS 的先卸载再装一致）
  if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)" != yes ]; then
    echo "install: ⚠️ 没有开启 linger：退出登录后服务会停，开机也不会自己起来。要常驻：sudo loginctl enable-linger $(id -un)" >&2
  fi
fi
echo "installed: $SERVICE_FILE"
echo "workroot: $WORKROOT"
