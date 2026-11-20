#!/usr/bin/env python3
"""按 vendor/bridge.lock.json 构建要打包进发版包的 lark-channel-bridge（只在发版机器上跑）。

  scripts/build_bridge.py --out <输出目录> [--cache <克隆缓存目录>] [--source <本地 fork 克隆>]

步骤：clone / fetch fork → 检出锁文件里的提交（核对 HEAD 和 package.json 版本）→ pnpm install --frozen-lockfile →
pnpm build（含 Web 控制台）→ 组装输出目录：package.json、LICENSE、README*、bin/、dist/、按锁文件装的扁平生产依赖
（pnpm --config.node-linker=hoisted，去掉 .bin、.pnpm、.modules.yaml 这类元数据）、THIRD_PARTY_NOTICES.md、BRIDGE.json。
输出目录里不能有链接；某个依赖既没有许可证文件也没有 license 字段就失败。需要 git、node、pnpm（corepack）和网络。
之后：release.py release --tag vX --key <主私钥> --bridge <输出目录>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
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


def load_lock(repo: Path = REPO) -> dict:
    lock = json.loads((repo / "vendor" / "bridge.lock.json").read_text(encoding="utf-8"))
    for k in ("repo", "commit", "version"):
        if not lock.get(k):
            raise BuildError("bridge.lock.json 缺 %s" % k)
    if len(lock["commit"]) != 40:
        raise BuildError("bridge.lock.json 的 commit 要写 40 位")
    return lock


def checkout(lock: dict, cache: Path, source: str = "") -> Path:
    src = cache / "src"
    if not (src / ".git").exists():
        cache.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", source or lock["repo"], str(src)])
    else:
        run(["git", "-C", str(src), "fetch", "--quiet", source or lock["repo"], "+refs/heads/*:refs/remotes/lock/*"])
    try:
        run(["git", "-C", str(src), "-c", "advice.detachedHead=false", "checkout", "--quiet", "--force", lock["commit"]])
    except BuildError:
        raise BuildError("fork 上没有提交 %s" % lock["commit"])
    head = run(["git", "-C", str(src), "rev-parse", "HEAD"]).strip()
    if head != lock["commit"]:
        raise BuildError("检出的是 %s，不是锁文件里的 %s" % (head, lock["commit"]))
    run(["git", "-C", str(src), "clean", "-fdxq", "-e", "node_modules"])
    ver = json.loads((src / "package.json").read_text(encoding="utf-8")).get("version")
    if ver != lock["version"]:
        raise BuildError("package.json 的版本是 %s，锁文件写的是 %s" % (ver, lock["version"]))
    return src


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


def build(out: Path, cache: Path, source: str = "", repo: Path = REPO) -> dict:
    lock = load_lock(repo)
    src = checkout(lock, cache, source)
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
    info = {"repo": lock["repo"], "commit": lock["commit"], "version": lock["version"], "tree_sha256": digest}
    (out / "BRIDGE.json").write_text(json.dumps(info, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build_bridge.py", description="按 vendor/bridge.lock.json 构建打包用的 bridge")
    ap.add_argument("--out", required=True, help="输出目录（会被清空重建）")
    ap.add_argument("--cache", default=str(Path.home() / ".larkyard-release" / "bridge-cache"), help="fork 克隆的缓存目录")
    ap.add_argument("--source", default="", help="从这个本地克隆取提交（默认锁文件里的仓库地址）")
    a = ap.parse_args(argv)
    try:
        info = build(Path(a.out).resolve(), Path(a.cache).expanduser(), a.source)
    except (BuildError, OSError, ValueError) as e:
        print("build_bridge: %s" % e, file=sys.stderr)
        return 1
    print("bridge %s（%s）→ %s\ntree_sha256 %s" % (info["version"], info["commit"], a.out, info["tree_sha256"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
