#!/bin/sh
# 把 Larkyard 的工作区约定装进 lark-channel-bridge 的工作目录。
# 用法：scripts/setup-workspace.sh <工作区目录> [--force]
#   - 生成 <工作区>/CLAUDE.md（由 workspace/CLAUDE.md.tmpl 渲染，填好本仓库路径）。
#     已有 CLAUDE.md 时不覆盖，除非加 --force（会先备份为 CLAUDE.md.bak-<时间>）。
#   - <工作区>/CLAUDE.local.md 不存在时，从 workspace/CLAUDE.local.example.md 复制一份；已存在则不动。
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -ge 1 ] || { echo "usage: $0 <workspace-dir> [--force]" >&2; exit 2; }
WS="$1"
FORCE="${2:-}"
[ -d "$WS" ] || { echo "setup-workspace: 目录不存在：$WS" >&2; exit 1; }
WS="$(cd "$WS" && pwd)"
case "$REPO" in *'|'*|*'&'*) echo "setup-workspace: 仓库路径里不能有 | 或 &" >&2; exit 1;; esac
if [ -e "$WS/CLAUDE.md" ]; then
  if [ "$FORCE" != "--force" ]; then
    echo "setup-workspace: $WS/CLAUDE.md 已存在；确认要替换时加 --force（会先备份）" >&2
    exit 1
  fi
  BAK="$WS/CLAUDE.md.bak-$(date +%Y%m%d%H%M%S)"
  cp -p "$WS/CLAUDE.md" "$BAK"
  echo "backup: $BAK"
fi
sed -e "s|@LARKYARD@|$REPO|g" "$REPO/workspace/CLAUDE.md.tmpl" > "$WS/CLAUDE.md"
echo "written: $WS/CLAUDE.md"
if [ ! -e "$WS/CLAUDE.local.md" ]; then
  cp "$REPO/workspace/CLAUDE.local.example.md" "$WS/CLAUDE.local.md"
  echo "created: $WS/CLAUDE.local.md（个人覆盖，按需修改）"
fi
