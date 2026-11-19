"""发版工具 scripts/release.py：可复现打包、三通道清单、序号和回拨检测、Ed25519 限制、回滚清单有效期、
暂停标记窗口、换钥清单、破坏性迁移警告、发布预签文件。对应 spec release-signing 的各个场景。临时 git 仓库 + 临时密钥。"""
from __future__ import annotations

import hashlib
import json
import os
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


if __name__ == "__main__":
    unittest.main()
