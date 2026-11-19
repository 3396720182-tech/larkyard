"""自动更新测试共用：临时 Ed25519 密钥、按发版工具的格式签清单、搭一个 file:// 更新源。"""
from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import time
from pathlib import Path

from helpers import REPO

from ticketd import updsig


def keygen(d: Path, name: str, kind: str = "ed25519") -> Path:
    key = d / name
    args = ["ssh-keygen", "-q", "-t", kind, "-N", "", "-C", name, "-f", str(key)]
    if kind == "rsa":
        args[2:2] = ["-b", "2048"]
    subprocess.run(args, check=True, capture_output=True)
    return key


def pub(key: Path) -> str:
    return Path(str(key) + ".pub").read_text(encoding="utf-8").strip()


def fp(key: Path) -> str:
    return updsig.fingerprint(pub(key))


def sign(data: bytes, key: Path) -> bytes:
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    import release
    return release.sign_bytes(data, key)


def package(version: str, files: dict = None) -> bytes:
    """一个最小的版本包：larkyard-<版本>/ 下的 ticketd/__init__.py、ticketd/launcher.py、bin/ticketd。"""
    files = dict(files or {})
    files.setdefault("ticketd/__init__.py", '__version__ = "%s"\n' % version)
    files.setdefault("ticketd/launcher.py", (REPO / "ticketd" / "launcher.py").read_text(encoding="utf-8"))
    files.setdefault("bin/ticketd", "#!/bin/sh\necho %s\n" % version)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        for name, text in sorted(files.items()):
            data = text.encode("utf-8") if isinstance(text, str) else text
            ti = tarfile.TarInfo("larkyard-%s/%s" % (version, name))
            ti.size, ti.mtime, ti.mode = len(data), 0, 0o755 if name.startswith("bin/") else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return raw.getvalue()


class Source:
    """file:// 更新源。seq 由调用方给（模拟发布者本机计数器）。"""

    def __init__(self, root: Path, key: Path):
        self.root = root
        self.key = key
        self.packages = {}

    @property
    def url(self) -> str:
        return self.root.as_uri()

    def add_package(self, version: str, data: bytes = None) -> bytes:
        data = data if data is not None else package(version)
        p = self.root / "packages" / ("larkyard-%s.tar.gz" % version)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.packages[version] = data
        return data

    def manifest(self, version: str, seq: int, channel: str = "stable", kind: str = "release", key: Path = None,
                 at: str = None, **extra) -> dict:
        if version not in self.packages:
            self.add_package(version)
        data = self.packages[version]
        doc = {"format": updsig.FORMAT, "product": updsig.PRODUCT, "kind": kind, "version": version,
               "package": "larkyard-%s.tar.gz" % version, "sha256": hashlib.sha256(data).hexdigest(),
               "size": len(data), "channel": channel, "min_from": "0.0.0", "commit": "0" * 40,
               "signed_at": updsig.iso(time.time()), "seq": seq, "notes": ""}
        doc.update(extra)
        self.put(at or "channels/%s/manifest.json" % channel, doc, key or self.key)
        return doc

    def put(self, rel: str, doc: dict, key: Path) -> None:
        data = updsig.canonical(doc)
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        Path(str(p) + ".sig").write_bytes(sign(data, key))

    def pause(self, seq: int, not_before: float, not_after: float, key: Path = None) -> None:
        self.put("pause.json", {"format": updsig.FORMAT, "product": updsig.PRODUCT, "kind": "pause", "seq": seq,
                                "not_before": updsig.iso(not_before), "not_after": updsig.iso(not_after),
                                "signed_at": updsig.iso(time.time())}, key or self.key)

    def rekey(self, seq: int, new_main: Path, key: Path, old_main: Path = None) -> None:
        self.put("rekey.json", {"format": updsig.FORMAT, "product": updsig.PRODUCT, "kind": "rekey", "seq": seq,
                                "new_main_key": " ".join(pub(new_main).split()[:2]),
                                "old_main_key": " ".join(pub(old_main or self.key).split()[:2]),
                                "signed_at": updsig.iso(time.time())}, key)
