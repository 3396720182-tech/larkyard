#!/bin/sh
# Larkyard 离线升级包的入口：release.py bundle 把它放进升级包根目录（叫 upgrade.sh），旁边是 feed/（本地发布目录）。
#   sh upgrade.sh --dry-run   只读检查：核对签名、列出要做的事，什么都不改
#   sh upgrade.sh             正式升级：在你自己的终端里执行，看完计划输入 yes
# 升级包放在哪儿都行（路径里有空格、中文、# % 都可以）：本目录的 feed/ 原样交给 install.sh --from-dir，
# 由 install.sh 按内置的发版公钥指纹验签，再执行 ticket migrate。通道取 feed/CHANNEL（bundle 写的），其他参数原样转给 install.sh。
set -eu
DIR="$(CDPATH='' cd -- "$(dirname -- "${0}")" && pwd -P)"
CHANNEL="$(cat "${DIR}/feed/CHANNEL" 2>/dev/null || true)"
case "${CHANNEL}" in stable|early|canary) ;; *) CHANNEL=stable;; esac
# ${1+"$@"}：不写 "${@}"，macOS 的 /bin/sh（bash 3.2）在 set -u 且没有参数时会报 unbound variable
exec /bin/sh "${DIR}/feed/install.sh" --from-dir "${DIR}/feed" --channel "${CHANNEL}" --upgrade-existing ${1+"$@"}
