#!/bin/sh
# Larkyard 一行安装：curl -fsSL <发布地址>/install.sh | sh
#   内部员工（控制台给的命令）：curl -fsSL <发布地址>/install.sh | sh -s -- --console https://<控制台> --enroll LY-XXXX-XXXX-XXXX
#
# 做的事：按下面写死的发版公钥指纹核对发布地址上的公钥 → 验签通道清单 → 下载并核对包 → 装成受管安装
# （~/.ticketd/app，能自动更新）→ 装 Node 和 lark-cli 到 ~/.ticketd → 进入 ticket setup（扫码建应用、认主人、装服务）。
# 公钥、签名、包的核对任何一步不通过就停下，~/.ticketd 下不写任何东西（之后 Node / lark-cli 下载失败时
# 会留下解好的版本目录，重新执行接着装）。可以重复执行：做过的步骤会跳过。
#
# 参数：
#   --from <地址>          发布地址（默认见 RELEASE_URL；测试可用 file://）
#   --from-dir <目录>      本地发布目录（离线升级包用）：任意路径都行（空格、中文、# % 等），脚本自己转成 file:// 地址
#   --channel <通道>       stable（默认）/ early / canary
#   --release-key SHA256:… / --backup-key SHA256:…   覆盖内置的两把发版公钥指纹
#   --profile <名字>       bridge 的 profile（默认 claude）
#   --workspace <目录>     工作区（默认 ~/larkyard-workspace）
#   --app-id <id>          用已有的飞书应用，不扫码新建
#   --console <地址> --enroll <入网码>   装完接着入网（内部员工）
#   --no-setup             只安装，不进向导（之后在终端里执行 ~/.ticketd/app/current/bin/ticket setup）
#   --upgrade-existing     已经在跑的旧安装（早期私有版、从 clone 装的）：装好之后用 ticket migrate 转成受管安装 +
#                          打包的 bridge（保留飞书应用、bridge 的 profile 和会话、工单数据库和工单目录）
#   --dry-run              和 --upgrade-existing 一起用：核对完只列出要做的事，~/.ticketd 下不写任何东西
#
# 发版公钥指纹：和开源仓库里的 install.sh 是同一份，两边可以互相比对（这就是「发布渠道以外」的核对）。
# 换钥（docs/spec.md §11「换钥」）之后两处一起改。
set -eu

RELEASE_URL="@RELEASE_URL@"
RELEASE_KEY_FP="SHA256:rr5QjPlEkIf0huivNLH2N/L4bsGCfmwYqPO6544TFvQ"
BACKUP_KEY_FP="SHA256:LXCbeB2nHOJq+4WXSxZU6EHOrvstEIikdYoUWQBx5rs"

say() { printf '%s\n' "$*" >&2; }
usage() {
  cat <<'USAGE'
Larkyard 一行安装：curl -fsSL <发布地址>/install.sh | sh [-s -- 参数]
  --from <地址>                 发布地址
  --from-dir <目录>             本地发布目录（离线升级包）
  --channel stable|early|canary 更新通道（默认 stable）
  --release-key / --backup-key  覆盖内置的两把发版公钥指纹（SHA256:…）
  --profile <名字>              bridge 的 profile（默认 claude）
  --workspace <目录>            工作区（默认 ~/larkyard-workspace）
  --app-id <id>                 用已有的飞书应用，不扫码新建
  --console <地址> --enroll <入网码>   装完接着入网（内部员工）
  --no-setup                    只安装，不进向导
  --upgrade-existing [--dry-run]  升级已经在跑的旧安装（--dry-run 只列出要做的事）
USAGE
}
die() { say "install: $*"; exit 1; }

case "${RELEASE_URL}" in @*) RELEASE_URL="";; esac
FROM="${LARKYARD_RELEASE_URL:-${RELEASE_URL}}"
CHANNEL=stable
FROM_SET=0
FROM_DIR=""; PROFILE=""; WORKSPACE=""; APP_ID=""; CONSOLE=""; ENROLL=""; SETUP=1; UPGRADE=0; DRY=0
while [ $# -gt 0 ]; do
  case "${1}" in
    --from) FROM="${2:?--from 要跟地址}"; FROM_SET=1; shift 2;;
    --from-dir) FROM_DIR="${2:?--from-dir 要跟目录}"; shift 2;;
    --channel) CHANNEL="${2:?}"; shift 2;;
    --release-key) RELEASE_KEY_FP="${2:?}"; shift 2;;
    --backup-key) BACKUP_KEY_FP="${2:?}"; shift 2;;
    --profile) PROFILE="${2:?}"; shift 2;;
    --workspace) WORKSPACE="${2:?}"; shift 2;;
    --app-id) APP_ID="${2:?}"; shift 2;;
    --console) CONSOLE="${2:?}"; shift 2;;
    --enroll) ENROLL="${2:?}"; shift 2;;
    --no-setup) SETUP=0; shift;;
    --upgrade-existing) UPGRADE=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "不认识的参数：${1}";;
  esac
done

if [ -n "${FROM_DIR}" ] && [ "${FROM_SET}" = 1 ]; then die "--from 和 --from-dir 只能用一个"; fi
case "${CHANNEL}" in stable|early|canary) ;; *) die "通道只能是 stable / early / canary";; esac
if [ -n "${ENROLL}" ] && [ -z "${CONSOLE}" ]; then die "--enroll 要和 --console <控制台地址> 一起用"; fi
if [ "${DRY}" = 1 ] && [ "${UPGRADE}" = 0 ]; then die "--dry-run 只和 --upgrade-existing 一起用"; fi
if [ "${UPGRADE}" = 1 ] && [ -n "${ENROLL}" ]; then die "--upgrade-existing 不和 --enroll 一起用（升级完再 ticket enroll）"; fi
if [ "${UPGRADE}" = 1 ] && [ "${DRY}" = 0 ] && [ "${LARK_CHANNEL:-}" = 1 ]; then
  die "升级会重启 ticketd 和 bridge：看起来是在 bridge 驱动的会话里执行（LARK_CHANNEL=1），会把这个会话自己打断。请在你自己的终端里执行（--dry-run 可以在哪儿都跑）"
fi
for fp in "${RELEASE_KEY_FP}" "${BACKUP_KEY_FP}"; do
  printf '%s' "${fp}" | grep -Eq '^SHA256:[A-Za-z0-9+/]{43}$' || die "公钥指纹格式不对：${fp}"
done

# ---------- 1. 环境 ----------
OS="$(uname -s)"
case "${OS}" in Darwin|Linux) ;; *) die "只支持 macOS 和 Linux / WSL2（当前 ${OS}）";; esac
case "$(uname -r)" in *Microsoft*) die "这是 WSL1：要 WSL2（PowerShell：wsl --set-version <发行版> 2）";; esac
for t in curl tar; do command -v "${t}" >/dev/null 2>&1 || die "缺少 ${t}"; done
KEYGEN=/usr/bin/ssh-keygen
[ -x "${KEYGEN}" ] || KEYGEN="$(command -v ssh-keygen || true)"
[ -n "${KEYGEN}" ] && [ -x "${KEYGEN}" ] || die "缺少 ssh-keygen（OpenSSH 客户端），验签要用"
# OpenSSH 8.1 之前的 ssh-keygen 没有 -Y：先探一下，免得后面报成「签名验证失败」
if "${KEYGEN}" -Y verify </dev/null 2>&1 | grep -qi "illegal option\|unknown option\|invalid option"; then
  die "ssh-keygen 太旧（不支持 -Y verify，要 OpenSSH 8.1+）：先升级 OpenSSH 客户端"
fi
PY="${TICKETD_PYTHON:-/usr/bin/python3}"
# 内联的 Python 一律 -I：不把当前目录（比如「下载」）放进 sys.path，那里的 pathlib.py / json.py 之类顶替不了标准库
[ -x "${PY}" ] || PY="$(command -v python3 || true)"
if [ -z "${PY}" ] || ! "${PY}" -I -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  if [ "${OS}" = Darwin ]; then die "需要 Python 3.9+：先执行 xcode-select --install"; fi
  die "需要 Python 3.9+（Ubuntu/Debian：sudo apt-get install python3）"
fi
# 发布地址（要用 Python 把本地目录转成 file:// 地址，所以放在找到 Python 之后）
if [ -n "${FROM_DIR}" ]; then
  [ -d "${FROM_DIR}" ] || die "本地发布目录不存在：${FROM_DIR}"
  # 整条路径按 RFC 3986 转义（空格、中文、# % ? 都不会被 curl 误解）
  FROM="$("${PY}" -I -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).resolve().as_uri())' "${FROM_DIR}")" \
    || die "本地发布目录转不成 file:// 地址：${FROM_DIR}"
  say "install: 用本地发布目录 ${FROM_DIR}"
else
  [ -n "${FROM}" ] || die "没有发布地址：用 --from https://<发布地址>"
  FROM="${FROM%/}"
  case "${FROM}" in
    https://*) ;;
    file://*)
      FROM="$(printf '%s' "${FROM}" | sed 's/ /%20/g')"  # curl 不收带空格的 file:// 地址（任意路径用 --from-dir）
      say "install: ⚠️ 用的是本地发布目录 ${FROM}（测试用）";;
    *) die "发布地址只接受 https://";;
  esac
fi
command -v claude >/dev/null 2>&1 || say "install: ⚠️ PATH 上没有 claude：先装好 Claude Code 并登录（https://docs.claude.com/claude-code），再执行 ticket setup"
if [ "${OS}" = Linux ]; then
  for t in bwrap socat; do command -v "${t}" >/dev/null 2>&1 || say "install: ⚠️ 缺少 ${t}（worker 沙箱要用；Ubuntu/Debian：sudo apt-get install bubblewrap socat）"; done
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/larkyard-install.XXXXXX")"
trap 'rm -rf "${TMP}"' EXIT INT TERM
fetch() { curl -fsSL --proto '=https,file' --proto-redir '=https' --max-time "${3:-120}" -o "${2}" "${1}"; }

# ---------- 2. 公钥 ----------
say "install: 取发版公钥并核对指纹"
fetch "${FROM}/keys/main.pub" "${TMP}/main.pub" 30 || die "取不到 ${FROM}/keys/main.pub"
fetch "${FROM}/keys/backup.pub" "${TMP}/backup.pub" 30 || die "取不到 ${FROM}/keys/backup.pub"
fp_of() { "${KEYGEN}" -l -E sha256 -f "${1}" 2>/dev/null | awk '{print $2}'; }
[ "$(fp_of "${TMP}/main.pub")" = "${RELEASE_KEY_FP}" ] || die "主公钥指纹不符（发布地址上的是 $(fp_of "${TMP}/main.pub")，应为 ${RELEASE_KEY_FP}）：停止安装"
[ "$(fp_of "${TMP}/backup.pub")" = "${BACKUP_KEY_FP}" ] || die "备份公钥指纹不符（发布地址上的是 $(fp_of "${TMP}/backup.pub")，应为 ${BACKUP_KEY_FP}）：停止安装"

# ---------- 3. 清单验签 ----------
say "install: 取 ${CHANNEL} 通道的清单并验签"
fetch "${FROM}/channels/${CHANNEL}/manifest.json" "${TMP}/manifest.json" 30 || die "取不到 ${CHANNEL} 通道的清单"
fetch "${FROM}/channels/${CHANNEL}/manifest.json.sig" "${TMP}/manifest.json.sig" 30 || die "取不到清单签名"
printf 'larkyard-release %s\n' "$(awk '{print $1" "$2}' "${TMP}/main.pub")" > "${TMP}/allowed_signers"
"${KEYGEN}" -Y verify -f "${TMP}/allowed_signers" -I larkyard-release -n larkyard-release \
  -s "${TMP}/manifest.json.sig" < "${TMP}/manifest.json" >/dev/null 2>&1 || die "清单签名验证失败：停止安装"

# ---------- 4. 包 ----------
INFO="$("${PY}" -I - "${TMP}/manifest.json" "${CHANNEL}" <<'PYEOF'
import json, re, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
ok = (m.get("format") == 1 and m.get("product") == "larkyard" and m.get("kind") in ("release", "rollback")
      and m.get("channel") == sys.argv[2] and re.fullmatch(r"\d+(\.\d+){1,3}", str(m.get("version", "")))
      and m.get("package") == "larkyard-%s.tar.gz" % m.get("version")
      and re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256", ""))) and isinstance(m.get("size"), int)
      and 0 < m["size"] <= 200 * 1024 * 1024)
if not ok:
    sys.exit(1)
print(m["version"], m["package"], m["sha256"], m["size"])
PYEOF
)" || die "清单内容不对（种类、产品、通道或包信息）：停止安装"
set -- ${INFO}
VERSION="${1}"; PACKAGE="${2}"; SHA="${3}"; SIZE="${4}"
say "install: 下载 Larkyard ${VERSION}"
fetch "${FROM}/packages/${PACKAGE}" "${TMP}/${PACKAGE}" 1800 || die "下载包失败"
"${PY}" -I - "${TMP}/${PACKAGE}" "${SHA}" "${SIZE}" <<'PYEOF' || die "包的大小或 SHA-256 和清单不一致：停止安装"
import hashlib, os, sys
p, sha, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
h = hashlib.sha256()
with open(p, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 16), b""):
        h.update(chunk)
sys.exit(0 if os.path.getsize(p) == size and h.hexdigest() == sha else 1)
PYEOF

# ---------- 5. 交给包里的代码装成受管安装 ----------
mkdir "${TMP}/x"
tar -xzf "${TMP}/${PACKAGE}" -C "${TMP}/x" "larkyard-${VERSION}/ticketd" || die "解包失败"
TH="${TICKETD_HOME:-${HOME}/.ticketd}"
NOURL=""
if [ "${UPGRADE}" = 1 ]; then
  # 先用包里的代码只读检查现有安装（不打开数据库、不写 ~/.ticketd）：有挡路的就停，什么都没装
  say "install: 检查现有安装"
  ( cd "${TMP}/x/larkyard-${VERSION}" && "${PY}" -m ticketd.migrate --check --target "${VERSION}" ${PROFILE:+--profile "${PROFILE}"} ) \
    || die "现有安装不能直接升级（原因见上）：~/.ticketd 下没有写任何东西"
  if [ "${DRY}" = 1 ]; then say "install: --dry-run：核对和检查都通过了，没有改任何东西"; exit 0; fi
  (exec </dev/tty) 2>/dev/null || die "升级要在终端里确认（这里没有终端）"
  printf '%s' "install: 照上面的计划升级吗？会重启 ticketd 和 bridge。输入 yes 继续：" >/dev/tty
  read -r ANSWER </dev/tty || ANSWER=""
  [ "${ANSWER}" = yes ] || die "没有改任何东西"
  INSTALLED_NOW=1
  if [ -L "${TH}/app/current" ]; then INSTALLED_NOW=0; fi
  NOURL=--upgrade-existing
fi
say "install: 安装到 ${TICKETD_HOME:-${HOME}/.ticketd}"
( cd "${TMP}/x/larkyard-${VERSION}" && "${PY}" -m ticketd.bootstrap --package "${TMP}/${PACKAGE}" \
    --manifest "${TMP}/manifest.json" --sig "${TMP}/manifest.json.sig" \
    --main-key "${TMP}/main.pub" --backup-key "${TMP}/backup.pub" \
    --main-fp "${RELEASE_KEY_FP}" --backup-fp "${BACKUP_KEY_FP}" --from "${FROM}" --channel "${CHANNEL}" \
    ${ENROLL:+--no-update-url} ${NOURL} ) || die "安装失败"
TICKET="${TICKETD_HOME:-${HOME}/.ticketd}/app/current/bin/ticket"

if [ "${UPGRADE}" = 1 ]; then
  # 更新源由 migrate 写（先备份，--undo 能撤）。本地发布目录（比如「下载」里）不写：守护进程读不到也可能被删，
  # 改写内置的正式发布地址（有的话），以后从那里自动更新
  UPDATE_URL="${FROM}"
  case "${FROM}" in file://*) UPDATE_URL="${RELEASE_URL}";; esac
  set -- migrate --yes --update-url "${UPDATE_URL}" --channel "${CHANNEL}"
  if [ "${INSTALLED_NOW}" = 1 ]; then set -- "$@" --installed-now; fi
  if [ -n "${PROFILE}" ]; then set -- "$@" --profile "${PROFILE}"; fi
  "${TICKET}" "$@" </dev/tty || die "升级没有完成（原因和要做的事见上）"
  say "install: 完成。自检：${TICKET} doctor"
  exit 0
fi

# ---------- 6. 向导 ----------
if [ "${SETUP}" = 0 ]; then
  say "install: 已安装。在终端里执行 ${TICKET} setup 完成扫码、认主人和装服务"
  exit 0
fi
if ! (exec </dev/tty) 2>/dev/null; then
  say "install: 已安装，但这里没有终端（CI、远程脚本），不能扫码。之后在终端里执行：${TICKET} setup"
  exit 0
fi
set -- setup
[ -n "${PROFILE}" ] && set -- "$@" --profile "${PROFILE}"
[ -n "${WORKSPACE}" ] && set -- "$@" --workspace "${WORKSPACE}"
[ -n "${APP_ID}" ] && set -- "$@" --app-id "${APP_ID}"
"${TICKET}" "$@" </dev/tty || die "向导没有完成：重新执行 ${TICKET} setup 会从没做完的那一步接着做"

# ---------- 7. 入网（内部员工） ----------
if [ -n "${ENROLL}" ]; then
  "${TICKET}" enroll "${CONSOLE}" "${ENROLL}" --release-key "${RELEASE_KEY_FP}" --backup-key "${BACKUP_KEY_FP}" </dev/tty \
    || die "入网没有完成：之后可以再执行 ${TICKET} enroll ${CONSOLE} <入网码> --release-key ${RELEASE_KEY_FP} --backup-key ${BACKUP_KEY_FP}"
fi
say "install: 完成"
