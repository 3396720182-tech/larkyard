"""发版工具 scripts/release.py：可复现打包、三通道清单、序号和回拨检测、Ed25519 限制、回滚清单有效期、
暂停标记窗口、换钥清单、破坏性迁移警告、发布预签文件。对应 spec release-signing 的各个场景。临时 git 仓库 + 临时密钥。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from helpers import REPO
import signing

sys.path.insert(0, str(REPO / "scripts"))
import release  # noqa: E402

from ticketd import updsig  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
                            GIT_AUTHOR_DATE="2026-11-01T00:00:00Z", GIT_COMMITTER_DATE="2026-11-01T00:00:00Z"))


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ticketd-release-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.saved)))
        os.environ["LARKYARD_RELEASE_HOME"] = str(self.tmp / "rh")
        self.repo = self.tmp / "repo"
        (self.repo / "ticketd").mkdir(parents=True)
        (self.repo / "bin").mkdir()
        git(self.repo, "init", "-q")
        self.write_version("0.3.0")
        (self.repo / "ticketd" / "db.py").write_text("SCHEMA = 'CREATE TABLE a(x)'\n")
        (self.repo / "bin" / "ticketd").write_text("#!/bin/sh\n")
        (self.repo / "bin" / "ticketd").chmod(0o755)
        (self.repo / ".gitignore").write_text("local.env\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        git(self.repo, "tag", "v0.3.0")
        (self.repo / "local.env").write_text("SECRET=1\n")  # 被忽略的本机文件
        (self.repo / "untracked.txt").write_text("x\n")
        self.keys = self.tmp / "keys"
        self.keys.mkdir()
        self.main = signing.keygen(self.keys, "main")
        self.backup = signing.keygen(self.keys, "backup")
        self.out = self.tmp / "out"

    def write_version(self, v):
        (self.repo / "ticketd" / "__init__.py").write_text('__version__ = "%s"\n' % v)

    def run_tool(self, *args):
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr
        o, e = StringIO(), StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = release.main(["--out", str(self.out)] + list(args))
        return code, o.getvalue(), e.getvalue()

    def release(self, tag="v0.3.0", *extra):
        return self.run_tool("release", "--tag", tag, "--key", str(self.main), "--repo", str(self.repo), *extra)

    def verify(self, path: Path, key: Path, principal=updsig.MAIN_PRINCIPAL) -> bool:
        signers = self.tmp / ("signers-%s" % key.name)
        signers.write_text("%s %s\n" % (principal, signing.pub(key)))
        return updsig.verify(path.read_bytes(), Path(str(path) + ".sig").read_bytes(), signers, principal)

    # ---------- 打包 ----------
    def test_package_contains_only_tracked_files(self):
        code, out, err = self.release()
        self.assertEqual(code, 0, err)
        pkg = self.out / "packages" / "larkyard-0.3.0.tar.gz"
        with tarfile.open(str(pkg)) as tf:
            names = sorted(m.name for m in tf.getmembers() if m.isfile())
            modes = {m.name: m.mode for m in tf.getmembers()}
            owners = {(m.uid, m.gid, m.uname, m.gname) for m in tf.getmembers()}
        self.assertEqual(names, ["larkyard-0.3.0/.gitignore", "larkyard-0.3.0/bin/ticketd",
                                 "larkyard-0.3.0/ticketd/__init__.py", "larkyard-0.3.0/ticketd/db.py"])
        self.assertEqual(modes["larkyard-0.3.0/bin/ticketd"], 0o755)
        self.assertEqual(owners, {(0, 0, "", "")})

    def test_same_tag_twice_same_sha(self):
        self.release()
        a = (self.out / "packages" / "larkyard-0.3.0.tar.gz").read_bytes()
        (self.out / "packages" / "larkyard-0.3.0.tar.gz").unlink()
        os.utime(str(self.repo / "ticketd" / "db.py"), (1, 1))  # 工作树的 mtime 不影响
        self.release()
        b = (self.out / "packages" / "larkyard-0.3.0.tar.gz").read_bytes()
        self.assertEqual(hashlib.sha256(a).hexdigest(), hashlib.sha256(b).hexdigest())

    def test_version_mismatch(self):
        git(self.repo, "tag", "v0.4.0")
        code, out, err = self.release("v0.4.0")
        self.assertNotEqual(code, 0)
        self.assertIn("版本号与 tag 不一致", err)
        self.assertFalse(self.out.exists())

    def test_missing_tag_and_dirty_tree(self):
        code, _, err = self.release("v9.9.9")
        self.assertNotEqual(code, 0)
        self.assertIn("不存在", err)
        (self.repo / "ticketd" / "db.py").write_text("changed\n")
        code, _, err = self.release()
        self.assertNotEqual(code, 0)
        self.assertIn("未提交", err)
        self.assertFalse(self.out.exists())

    # ---------- 清单 ----------
    def test_three_channels_complete_fields_and_verify(self):
        code, out, err = self.release("v0.3.0", "--min-from", "0.2.0", "--notes", "修 bug")
        self.assertEqual(code, 0, err)
        pkg = (self.out / "packages" / "larkyard-0.3.0.tar.gz").read_bytes()
        commit = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "v0.3.0^{commit}"], capture_output=True,
                                text=True).stdout.strip()
        seqs = []
        for ch in updsig.CHANNELS:
            p = self.out / "signed" / "0.3.0" / ("%s.json" % ch)
            raw = p.read_bytes()
            doc = json.loads(raw)
            self.assertEqual(updsig.canonical(doc), raw)  # 确定性序列化
            for k in ("format", "product", "kind", "version", "package", "sha256", "size", "channel", "min_from",
                      "commit", "signed_at", "seq", "notes"):
                self.assertIn(k, doc)
            self.assertEqual((doc["kind"], doc["channel"], doc["version"], doc["min_from"]),
                             ("release", ch, "0.3.0", "0.2.0"))
            self.assertEqual(doc["sha256"], hashlib.sha256(pkg).hexdigest())
            self.assertEqual(doc["size"], len(pkg))
            self.assertEqual(doc["commit"], commit)
            self.assertTrue(self.verify(p, self.main))
            self.assertFalse(self.verify(p, self.backup))
            seqs.append(doc["seq"])
        self.assertEqual(seqs, [1, 2, 3])
        self.release()  # 再发一次：序号继续往上
        self.assertEqual(json.loads((self.out / "signed" / "0.3.0" / "canary.json").read_text())["seq"], 4)

    def test_counter_rolled_back(self):
        self.release()
        (self.tmp / "rh" / "seq").write_text("1\n")
        code, _, err = self.release()
        self.assertNotEqual(code, 0)
        self.assertIn("序号回退", err)
        self.assertEqual(len((self.tmp / "rh" / "signlog.jsonl").read_text().splitlines()), 3)

    def test_rsa_key_rejected(self):
        rsa = signing.keygen(self.keys, "rsa", "rsa")
        code, _, err = self.run_tool("release", "--tag", "v0.3.0", "--key", str(rsa), "--repo", str(self.repo))
        self.assertNotEqual(code, 0)
        self.assertIn("只接受 Ed25519", err)
        self.assertFalse(self.out.exists())

    def test_private_key_never_written(self):
        self.release()
        secret = self.main.read_bytes()
        body = b"".join(secret.splitlines()[1:-1])
        for p in list(self.out.rglob("*")) + list((self.tmp / "rh").rglob("*")):
            if p.is_file():
                data = p.read_bytes()
                self.assertNotIn(body[:40], data.replace(b"\n", b""), p)
        self.assertNotIn("serve", release.COMMANDS)

    # ---------- 回滚 / 暂停 / 换钥 ----------
    def test_rollback_manifest(self):
        self.release()
        self.write_version("0.3.1")
        git(self.repo, "commit", "-qam", "bump")
        git(self.repo, "tag", "v0.3.1")
        self.release("v0.3.1")
        code, out, err = self.run_tool("rollback", "--version", "0.3.0", "--channel", "stable", "--key", str(self.main))
        self.assertEqual(code, 0, err)
        p = next((self.out / "signed" / "0.3.0").glob("rollback-stable-*.json"))
        doc = json.loads(p.read_text())
        self.assertEqual((doc["kind"], doc["version"], doc["channel"]), ("rollback", "0.3.0", "stable"))
        self.assertEqual(doc["seq"], 7)
        self.assertAlmostEqual(updsig.parse_iso(doc["expires_at"]) - updsig.parse_iso(doc["signed_at"]), 72 * 3600,
                               delta=2)
        rel = json.loads((self.out / "signed" / "0.3.0" / "stable.json").read_text())
        self.assertEqual(doc["sha256"], rel["sha256"])
        self.assertTrue(self.verify(p, self.main))

    def test_rollback_validity_limit(self):
        self.release()
        code, _, err = self.run_tool("rollback", "--version", "0.3.0", "--channel", "stable", "--key", str(self.main),
                                     "--hours", str(30 * 24))
        self.assertNotEqual(code, 0)
        self.assertIn("最长 7 天", err)

    def test_rollback_only_for_released_versions(self):
        code, _, err = self.run_tool("rollback", "--version", "0.1.0", "--channel", "stable", "--key", str(self.main))
        self.assertNotEqual(code, 0)

    def test_pause_batch(self):
        code, out, err = self.run_tool("pause", "--key", str(self.main), "--start", "2026-12-01T00:00:00Z")
        self.assertEqual(code, 0, err)
        files = sorted((self.out / "signed" / "pause").glob("*.json"))
        self.assertEqual(len(files), 12)
        windows = [json.loads(p.read_text()) for p in files]
        for i, w in enumerate(windows):
            nb, na = updsig.parse_iso(w["not_before"]), updsig.parse_iso(w["not_after"])
            self.assertEqual(na - nb, 7 * 86400)
            if i:
                self.assertEqual(nb, updsig.parse_iso(windows[i - 1]["not_after"]))
            self.assertTrue(self.verify(files[i], self.main))

    def test_rekey_signed_by_backup_only(self):
        new_main = signing.keygen(self.keys, "main2")
        code, out, err = self.run_tool("rekey", "--backup-key", str(self.backup), "--new-main", str(new_main) + ".pub",
                                       "--old-main", str(self.main) + ".pub")
        self.assertEqual(code, 0, err)
        p = next((self.out / "signed").glob("rekey-*.json"))
        doc = json.loads(p.read_text())
        self.assertEqual(doc["new_main_key"], " ".join(signing.pub(new_main).split()[:2]))
        self.assertEqual(doc["old_main_key"], " ".join(signing.pub(self.main).split()[:2]))
        self.assertTrue(self.verify(p, self.backup, updsig.BACKUP_PRINCIPAL))
        self.assertFalse(self.verify(p, self.main, updsig.BACKUP_PRINCIPAL))

    # ---------- 迁移检查 ----------
    def test_destructive_migration_warns(self):
        self.write_version("0.3.1")
        (self.repo / "ticketd" / "db.py").write_text("SCHEMA = 'CREATE TABLE a(x)'\nX = 'ALTER TABLE a DROP COLUMN x'\n")
        git(self.repo, "commit", "-qam", "drop")
        git(self.repo, "tag", "v0.3.1")
        code, out, err = self.release("v0.3.1")
        self.assertEqual(code, 0, err)
        self.assertIn("DROP / RENAME", out)
        code, out, err = self.release("v0.3.0")
        self.assertNotIn("DROP / RENAME", out)

    # ---------- 发布 ----------
    def test_publish_lays_out_source_and_client_accepts(self):
        self.release()
        src = self.tmp / "source"
        code, out, err = self.run_tool("publish", str(self.out / "signed" / "0.3.0" / "canary.json"), "--to", str(src))
        self.assertEqual(code, 0, err)
        self.assertTrue((src / "channels" / "canary" / "manifest.json").is_file())
        self.assertTrue((src / "channels" / "canary" / "manifest.json.sig").is_file())
        self.assertTrue((src / "packages" / "larkyard-0.3.0.tar.gz").is_file())
        self.assertFalse((src / "channels" / "stable").exists())  # 推进到 stable 只是再发布那份已签好的
        self.run_tool("publish", str(self.out / "signed" / "0.3.0" / "stable.json"), "--to", str(src))
        self.assertTrue(self.verify(src / "channels" / "stable" / "manifest.json", self.main))
        self.run_tool("pause", "--key", str(self.main), "--weeks", "1")
        p = next((self.out / "signed" / "pause").glob("*.json"))
        self.run_tool("publish", str(p), "--to", str(src))
        self.assertTrue((src / "pause.json.sig").is_file())
        self.run_tool("unpublish-pause", "--to", str(src))
        self.assertFalse((src / "pause.json").exists())

    def test_publish_refuses_presigned_manifest_older_than_live(self):
        """发版后在 stable 上发过回滚清单：预签的 stable 清单序号更小，客户端会拒，publish 直接拦下并提示重签。"""
        self.release()
        self.write_version("0.3.1")
        git(self.repo, "commit", "-qam", "bump")
        git(self.repo, "tag", "v0.3.1")
        self.release("v0.3.1")
        src = self.tmp / "source"
        code, _, err = self.run_tool("rollback", "--version", "0.3.0", "--channel", "stable", "--key", str(self.main))
        self.assertEqual(code, 0, err)
        rb = next((self.out / "signed" / "0.3.0").glob("rollback-stable-*.json"))
        self.assertEqual(self.run_tool("publish", str(rb), "--to", str(src))[0], 0)
        code, _, err = self.run_tool("publish", str(self.out / "signed" / "0.3.1" / "stable.json"), "--to", str(src))
        self.assertNotEqual(code, 0)
        self.assertIn("重新签", err)
        self.assertEqual(json.loads((src / "channels" / "stable" / "manifest.json").read_text())["kind"], "rollback")


if __name__ == "__main__":
    unittest.main()


class ReleaseBridgeTest(unittest.TestCase):
    """tag 里有 bridge/ 源码（T0112 起）：必须给 --bridge，BRIDGE.json 的 commit 要等于 <tag>:bridge 的树对象号，
    放进 vendor/bridge/，源码本身不进包，可复现。"""
    run_tool = ReleaseTest.run_tool
    release = ReleaseTest.release
    write_version = ReleaseTest.write_version

    def setUp(self):
        ReleaseTest.setUp(self)
        from ticketd import bridge
        self.bridge_mod = bridge
        (self.repo / "bridge" / "src").mkdir(parents=True)
        (self.repo / "bridge" / "package.json").write_text('{"name": "b", "version": "0.7.1"}\n')
        (self.repo / "bridge" / "src" / "index.ts").write_text("export const x = 1;\n")
        self.write_version("0.3.1")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "v")
        git(self.repo, "tag", "v0.3.1")
        self.tree = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "v0.3.1:bridge"], capture_output=True,
                                   text=True, check=True).stdout.strip()
        self.bdir = self.tmp / "bridge-out"
        (self.bdir / "bin").mkdir(parents=True)
        (self.bdir / "bin" / "lark-channel-bridge.mjs").write_text("// entry\n")
        (self.bdir / "node_modules" / "dep").mkdir(parents=True)
        (self.bdir / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n")
        self.stamp()

    def stamp(self, commit=None):
        info = {"repo": "larkyard", "commit": commit or self.tree, "version": "0.7.1",
                "tree_sha256": self.bridge_mod.tree_digest(self.bdir)}
        (self.bdir / "BRIDGE.json").write_text(json.dumps(info))

    def test_bridge_required_and_packed(self):
        code, out, err = self.release("v0.3.1")
        self.assertEqual(code, 1)
        self.assertIn("build_bridge.py", err)
        code, out, err = self.release("v0.3.1", "--bridge", str(self.bdir))
        self.assertEqual(code, 0, err)
        pkg = self.out / "packages" / "larkyard-0.3.1.tar.gz"
        with tarfile.open(str(pkg)) as tf:
            names = {m.name for m in tf.getmembers() if m.isfile()}
            self.assertFalse([m.name for m in tf.getmembers() if m.issym() or m.islnk()])
        self.assertIn("larkyard-0.3.1/vendor/bridge/bin/lark-channel-bridge.mjs", names)
        self.assertIn("larkyard-0.3.1/vendor/bridge/node_modules/dep/index.js", names)
        self.assertIn("larkyard-0.3.1/vendor/bridge/BRIDGE.json", names)
        self.assertFalse([n for n in names if n.startswith("larkyard-0.3.1/bridge/")], "源码不进包")
        self.assertIn("larkyard-0.3.1/ticketd/__init__.py", names)
        a = pkg.read_bytes()
        pkg.unlink()
        self.assertEqual(self.release("v0.3.1", "--bridge", str(self.bdir))[0], 0)
        self.assertEqual(a, pkg.read_bytes())
        # 老客户端的解包规则能装带 bridge 的包
        from ticketd import update
        dest = self.tmp / "x"
        update.extract(pkg, dest, "larkyard-0.3.1")
        self.assertEqual(self.bridge_mod.tree_digest(dest / "vendor" / "bridge"),
                         json.loads((self.bdir / "BRIDGE.json").read_text())["tree_sha256"])

    def test_wrong_source_or_modified_tree_refused(self):
        self.stamp(commit="d" * 40)
        code, out, err = self.release("v0.3.1", "--bridge", str(self.bdir))
        self.assertEqual(code, 1)
        self.assertIn("重新构建", err)
        self.stamp()
        (self.bdir / "node_modules" / "dep" / "index.js").write_text("evil\n")
        code, out, err = self.release("v0.3.1", "--bridge", str(self.bdir))
        self.assertEqual(code, 1)
        self.assertIn("不一致", err)
        self.assertFalse((self.out / "packages" / "larkyard-0.3.1.tar.gz").exists())

    def test_bridge_for_tag_without_source_refused(self):
        code, out, err = self.release("v0.3.0", "--bridge", str(self.bdir))
        self.assertEqual(code, 1)
        self.assertIn("没有 bridge/", err)

    def test_older_tag_with_lock_file_still_checked(self):
        """T0112 以前的 tag 只有 vendor/bridge.lock.json：仍按锁文件里的提交核对（重打老版本用）。"""
        git(self.repo, "checkout", "-q", "v0.3.0")
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "bridge.lock.json").write_text(json.dumps({"repo": "x", "commit": "c" * 40,
                                                                          "version": "0.7.1"}))
        self.write_version("0.2.9")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "old")
        git(self.repo, "tag", "v0.2.9")
        code, out, err = self.release("v0.2.9", "--bridge", str(self.bdir))
        self.assertEqual(code, 1)
        self.assertIn("重新构建", err)
        self.stamp(commit="c" * 40)
        code, out, err = self.release("v0.2.9", "--bridge", str(self.bdir))
        self.assertEqual(code, 0, err)


class PublishInstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ticketd-pi-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.main = signing.keygen(self.tmp, "main")
        self.backup = signing.keygen(self.tmp, "backup")
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        text = re.sub(r'^RELEASE_KEY_FP=".*"$', 'RELEASE_KEY_FP="%s"' % signing.fp(self.main), text, flags=re.M)
        text = re.sub(r'^BACKUP_KEY_FP=".*"$', 'BACKUP_KEY_FP="%s"' % signing.fp(self.backup), text, flags=re.M)
        (self.repo / "install.sh").write_text(text, encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "i")

    def run_pi(self, *extra):
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr
        o, e = StringIO(), StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            code = release.main(["publish-installer", "--repo", str(self.repo), "--to", str(self.tmp / "site")]
                                + list(extra))
        return code, o.getvalue(), e.getvalue()

    def test_fills_url_and_keys(self):
        code, out, err = self.run_pi("--url", "https://dl.example.com/ly/", "--main", str(self.main) + ".pub",
                                     "--backup", str(self.backup) + ".pub")
        self.assertEqual(code, 0, err)
        site = self.tmp / "site"
        self.assertIn('RELEASE_URL="https://dl.example.com/ly"', (site / "install.sh").read_text(encoding="utf-8"))
        self.assertEqual((site / "keys" / "main.pub").read_text().strip(),
                         " ".join(signing.pub(self.main).split()[:2]))
        self.assertTrue(os.access(str(site / "install.sh"), os.X_OK))

    def test_fingerprint_mismatch_refused(self):
        code, out, err = self.run_pi("--url", "https://dl.example.com", "--main", str(self.backup) + ".pub",
                                     "--backup", str(self.main) + ".pub")
        self.assertEqual(code, 1)
        self.assertIn("指纹", err)
        self.assertFalse((self.tmp / "site" / "install.sh").exists())

    def test_unsafe_url_and_dirty_tree_refused(self):
        keys = ("--main", str(self.main) + ".pub", "--backup", str(self.backup) + ".pub")
        code, out, err = self.run_pi("--url", "https://acct.blob.core.windows.net/$web", *keys)
        self.assertEqual(code, 1)
        (self.repo / "install.sh").write_text("echo changed\n", encoding="utf-8")
        code, out, err = self.run_pi("--url", "https://dl.example.com", *keys)
        self.assertEqual(code, 1)
        self.assertIn("未提交", err)

    def test_repo_install_sh_fingerprints_are_well_formed(self):
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        for var in ("RELEASE_KEY_FP", "BACKUP_KEY_FP"):
            m = re.search(r'^%s="(.*)"$' % var, text, re.M)
            self.assertTrue(m and updsig.FINGERPRINT_RE.match(m.group(1)), var)
        self.assertIn('RELEASE_URL="@RELEASE_URL@"', text)
