#!/usr/bin/env python3
"""从仓库里的 bridge/ 源码构建要打包进发版包的 bridge（只在发版机器上跑）。

  scripts/build_bridge.py --out <输出目录> [--ref <提交或 tag，默认 HEAD>] [--work <构建目录>]

步骤：取 <ref> 里 bridge/ 的 git 树（只用已提交的内容，不读工作树）→ 解到构建目录 → pnpm install --frozen-lockfile →
pnpm build（含 Web 控制台）→ 组装输出目录：package.json、LICENSE、README*、bin/、dist/、按锁文件装的扁平生产依赖
（pnpm --config.node-linker=hoisted，去掉 .bin、.pnpm、.modules.yaml 这类元数据）、THIRD_PARTY_NOTICES.md、BRIDGE.json。
BRIDGE.json 的 commit 是 <ref>:bridge 的 git 树对象号（40 位）：源码一个字节不变它就不变，release.py 按它核对。
输出目录里不能有链接；某个依赖既没有许可证文件也没有 license 字段就失败。需要 git、node、pnpm（corepack）和网络。
之后：release.py release --tag vX --key <主私钥> --bridge <输出目录>（--ref 要和 tag 指向同一份 bridge/）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ticketd.bridge import BridgeError, tree_digest  # noqa: E402

KEEP = ("package.json", "LICENSE", "README.md", "README.zh.md", "bin", "dist")
LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")


class BuildError(Exception):
    pass


def run(argv, cwd=None, env=None) -> str:
    p = subprocess.run(argv, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise BuildError("%s 失败：%s" % (" ".join(argv[:3]), (p.stderr or p.stdout).strip()[-800:]))
    return p.stdout


SOURCE_DIR = "bridge"


def source_tree(repo: Path, ref: str) -> str:
    """<ref>:bridge 的树对象号；ref 里没有 bridge/ 就失败。"""
    try:
        tree = run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "%s:%s" % (ref, SOURCE_DIR)]).strip()
        kind = run(["git", "-C", str(repo), "cat-file", "-t", tree]).strip()
    except BuildError:
        raise BuildError("%s 里没有 %s/ 源码" % (ref, SOURCE_DIR))
    if kind != "tree" or len(tree) != 40:
        raise BuildError("%s:%s 的树对象号不对：%s" % (ref, SOURCE_DIR, tree))
    return tree


def export_source(repo: Path, ref: str, work: Path) -> tuple:
    """把 <ref>:bridge 解到 work/src（先清空）。返回 (树对象号, package.json 版本, 源码目录)。"""
    tree = source_tree(repo, ref)
    src = work / "src"
    if src.exists():
        shutil.rmtree(str(src))
    src.mkdir(parents=True)
    p = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", tree], capture_output=True)
    if p.returncode != 0:
        raise BuildError("git archive 失败：%s" % p.stderr.decode("utf-8", "replace").strip()[-800:])
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
        for m in tf.getmembers():
            parts = m.name.split("/")
            if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                raise BuildError("bridge 源码里有不支持的条目：%s" % m.name)
        tf.extractall(str(src))
    ver = json.loads((src / "package.json").read_text(encoding="utf-8")).get("version")
    if not ver:
        raise BuildError("bridge/package.json 没有 version")
    return tree, ver, src


def notices(node_modules: Path) -> str:
    """每个生产依赖一节：名称、版本、许可证文本（没有许可证文件时写 package.json 的 license 字段）。"""
    parts = ["# 第三方声明\n\n打包的 lark-channel-bridge 的生产依赖及其许可证。\n"]
    pkgs = []
    for pj in sorted(node_modules.glob("*/package.json")) + sorted(node_modules.glob("@*/*/package.json")):
        pkgs.append(pj.parent)
    for d in sorted(pkgs, key=lambda p: p.relative_to(node_modules).as_posix()):
        meta = json.loads((d / "package.json").read_text(encoding="utf-8"))
        name, ver = meta.get("name") or d.name, meta.get("version") or "?"
        lic = meta.get("license")
        if isinstance(lic, dict):
            lic = lic.get("type")
        files = sorted(p for p in d.iterdir() if p.is_file() and p.name.upper().split(".")[0] in LICENSE_NAMES)
        if not files and not lic:
            raise BuildError("%s@%s 既没有许可证文件也没有 license 字段" % (name, ver))
        parts.append("\n## %s@%s（%s）\n" % (name, ver, lic or "见下"))
        for f in files:
            parts.append("\n```\n%s\n```\n" % f.read_text(encoding="utf-8", errors="replace").strip())
    return "".join(parts)


def build(out: Path, work: Path, ref: str = "HEAD", repo: Path = REPO) -> dict:
    tree, version, src = export_source(repo, ref, work)
    env = dict(os.environ, CI="1")
    run(["pnpm", "install", "--frozen-lockfile"], cwd=src, env=env)
    run(["pnpm", "build"], cwd=src, env=env)
    if out.exists():
        shutil.rmtree(str(out))
    out.mkdir(parents=True)
    for name in KEEP:
        p = src / name
        if p.is_dir():
            shutil.copytree(str(p), str(out / name), symlinks=True)
        elif p.is_file():
            shutil.copy2(str(p), str(out / name))
    with tempfile.TemporaryDirectory(prefix="bridge-prod-") as td:
        stage = Path(td)
        for name in ("package.json", "pnpm-lock.yaml"):
            shutil.copy2(str(src / name), str(stage / name))
        run(["pnpm", "install", "--prod", "--frozen-lockfile", "--ignore-scripts", "--config.node-linker=hoisted"],
            cwd=stage, env=env)
        nm = stage / "node_modules"
        for meta in (".bin", ".pnpm", ".modules.yaml"):
            p = nm / meta
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(str(p))
            elif p.exists() or p.is_symlink():
                p.unlink()
        for p in list(nm.glob(".pnpm-*")):
            p.unlink()
        for dirpath, dirnames, filenames in os.walk(str(nm)):
            for n in dirnames + filenames:  # 依赖里自带的 .bin 链接等
                q = Path(dirpath) / n
                if q.is_symlink():
                    q.unlink()
        shutil.copytree(str(nm), str(out / "node_modules"))
        (out / "THIRD_PARTY_NOTICES.md").write_text(notices(out / "node_modules"), encoding="utf-8")
    try:
        digest = tree_digest(out)
    except BridgeError as e:
        raise BuildError(str(e))
    info = {"repo": "larkyard", "source": SOURCE_DIR + "/", "commit": tree, "version": version, "tree_sha256": digest}
    (out / "BRIDGE.json").write_text(json.dumps(info, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build_bridge.py", description="从仓库里的 bridge/ 源码构建打包用的 bridge")
    ap.add_argument("--out", required=True, help="输出目录（会被清空重建）")
    ap.add_argument("--ref", default="HEAD", help="取这个提交或 tag 里的 bridge/（默认 HEAD）")
    ap.add_argument("--work", default=str(Path.home() / ".larkyard-release" / "bridge-build"), help="构建目录（每次清空源码）")
    a = ap.parse_args(argv)
    try:
        info = build(Path(a.out).resolve(), Path(a.work).expanduser(), a.ref)
    except (BuildError, OSError, ValueError) as e:
        print("build_bridge: %s" % e, file=sys.stderr)
        return 1
    print("bridge %s（%s）→ %s\ntree_sha256 %s" % (info["version"], info["commit"], a.out, info["tree_sha256"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
