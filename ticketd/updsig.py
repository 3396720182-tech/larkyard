"""自动更新的签名和格式（openspec add-signed-self-update）：清单的确定性序列化、版本号、时间、
用系统自带的 `ssh-keygen -Y verify` 验签、按带外指纹核对后写入信任的公钥。只用标准库，客户端和发版工具共用。"""
from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

NAMESPACE = "larkyard-release"          # 签名命名空间：别处的 SSH 签名冒充不了
MAIN_PRINCIPAL = "larkyard-release"     # main.allowed_signers 里的身份：验普通清单、回滚清单、暂停标记
BACKUP_PRINCIPAL = "larkyard-rekey"     # backup.allowed_signers 里的身份：只验换钥清单
PRODUCT = "larkyard"
FORMAT = 1
CHANNELS = ("canary", "early", "stable")
KINDS = ("release", "rollback", "rekey", "pause")
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")
ED25519_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com")  # check-private: ignore（密钥类型名，不是邮箱）
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")

# 探测 `ssh-keygen -Y verify` 是否可用且行为正确：一把只为探测生成的公钥（私钥已丢弃）对 PROBE_DATA 的签名
PROBE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEHhvSHQKqdtKyyf3mZbNoZjVNfB/czBWx4/Ab39Jdjd"
PROBE_DATA = b"larkyard ssh-keygen probe\n"
PROBE_SIG = """-----BEGIN SSH SIGNATURE-----
U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgQeG9IdAqp20rLJ/eZls2hmNU18
H9zMFbHj8Bvf0l2N0AAAAQbGFya3lhcmQtcmVsZWFzZQAAAAAAAAAGc2hhNTEyAAAAUwAA
AAtzc2gtZWQyNTUxOQAAAEAIteBkwVMTbVQK25hJP9y51W3nmt5lRtYOPfSbkvm/6lLCB4
eQT3zqmuwax3j3Va80gQa+KtTekt6w2BZDfkkC
-----END SSH SIGNATURE-----
"""


def canonical(obj: dict) -> bytes:
    """清单的确定性 UTF-8 JSON：键排序、无多余空白。签名针对这份字节。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def parse_iso(s) -> float:
    if not isinstance(s, str):
        raise ValueError("时间不是字符串：%r" % (s,))
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


def version_tuple(v) -> tuple:
    if not isinstance(v, str) or not VERSION_RE.match(v):
        raise ValueError("版本号格式不对：%r" % (v,))
    return tuple(int(x) for x in v.split("."))


def cmp_version(a: str, b: str) -> int:
    ta, tb = version_tuple(a), version_tuple(b)
    n = max(len(ta), len(tb))
    ta, tb = ta + (0,) * (n - len(ta)), tb + (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def ssh_keygen() -> str:
    return shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"


def verify(data: bytes, sig: bytes, signers: Path, principal: str) -> bool:
    """用 allowed_signers 文件和固定命名空间验签。任何异常都当验不过。"""
    if not signers.is_file() or not sig:
        return False
    with tempfile.TemporaryDirectory(prefix="larkyard-verify-") as d:
        sp = Path(d) / "data.sig"
        sp.write_bytes(sig)
        try:
            p = subprocess.run([ssh_keygen(), "-Y", "verify", "-f", str(signers), "-I", principal, "-n", NAMESPACE,
                                "-s", str(sp)], input=data, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return p.returncode == 0


_probe_cache: dict = {}


def verify_available() -> bool:
    """系统的 ssh-keygen 支持 `-Y verify`（OpenSSH ≥ 8.1），而且真能验过一份已知签名、验不过篡改过的数据。"""
    exe = ssh_keygen()
    if exe in _probe_cache:
        return _probe_cache[exe]
    ok = False
    with tempfile.TemporaryDirectory(prefix="larkyard-probe-") as d:
        signers = Path(d) / "allowed_signers"
        signers.write_text("%s %s\n" % (MAIN_PRINCIPAL, PROBE_KEY), encoding="utf-8")
        ok = verify(PROBE_DATA, PROBE_SIG.encode(), signers, MAIN_PRINCIPAL) and \
            not verify(PROBE_DATA + b"x", PROBE_SIG.encode(), signers, MAIN_PRINCIPAL)
    _probe_cache[exe] = ok
    return ok


def parse_pubkey(text: str) -> str:
    """只接受一行 Ed25519 公钥（`ssh-ed25519 <base64> [注释]`，硬件密钥见 ED25519_TYPES），
    返回去掉注释的 `类型 base64`。"""
    parts = (text or "").strip().split()
    if len(parts) < 2 or parts[0] not in ED25519_TYPES or not re.match(r"^[A-Za-z0-9+/=]+$", parts[1]):
        raise ValueError("只接受一行 Ed25519 公钥（ssh-ed25519 开头）")
    return "%s %s" % (parts[0], parts[1])


def fingerprint(pubkey: str) -> str:
    """`ssh-keygen -lf` 算出的 SHA256 指纹。"""
    with tempfile.TemporaryDirectory(prefix="larkyard-fp-") as d:
        p = Path(d) / "key.pub"
        p.write_text(parse_pubkey(pubkey) + "\n", encoding="utf-8")
        out = subprocess.run([ssh_keygen(), "-l", "-E", "sha256", "-f", str(p)], capture_output=True, text=True,
                             timeout=30)
    fields = out.stdout.split()
    if out.returncode != 0 or len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise ValueError("算不出公钥指纹：%s" % (out.stderr.strip() or out.stdout.strip()))
    return fields[1]


def read_pubkey_arg(arg: str) -> str:
    """命令行给的公钥：可以是文件路径，也可以直接是公钥内容。"""
    if arg and not arg.strip().startswith("ssh-") and os.path.isfile(os.path.expanduser(arg)):
        return Path(os.path.expanduser(arg)).read_text(encoding="utf-8")
    return arg


def write_signers(path: Path, principal: str, pubkey: str) -> None:
    """原子写入 allowed_signers（权限 600）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path.parent), 0o700)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("%s %s\n" % (principal, parse_pubkey(pubkey)))
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def signer_key(path: Path) -> Optional[str]:
    try:
        parts = path.read_text(encoding="utf-8").split()
    except OSError:
        return None
    return "%s %s" % (parts[1], parts[2]) if len(parts) >= 3 else None
