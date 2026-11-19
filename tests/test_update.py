"""自动更新客户端（ticketd/update.py）：启用条件、信任公钥的指纹核对、验签和校验、防重放、降级保护、换钥、暂停。
对应 openspec/changes/add-signed-self-update/specs/self-update/spec.md 的各个场景。用临时密钥和 file:// 更新源，不碰网络。"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import TicketdTestCase
import signing

from ticketd import config, metrics, update, updsig


class Keys:
    """整个模块共用一套密钥（生成要几十毫秒，用例很多）。"""
    dir = None

    @classmethod
    def get(cls):
        if cls.dir is None:
            cls.dir = Path(tempfile.mkdtemp(prefix="ticketd-keys-"))
            for n in ("main", "backup", "evil", "main2"):
                setattr(cls, n, signing.keygen(cls.dir, n))
            cls.rsa = signing.keygen(cls.dir, "rsa", "rsa")
        return cls


def tearDownModule():
    if Keys.dir is not None:
        shutil.rmtree(str(Keys.dir), ignore_errors=True)


def args(action, **kw):
    base = dict(action=action, main=None, backup=None, fingerprint=None, backup_fingerprint=None, days=None,
                undo=False)
    base.update(kw)
    return SimpleNamespace(**base)


class UpdateBase(TicketdTestCase):
    def setUp(self):
        super().setUp()
        self.k = Keys.get()
        self.src = signing.Source(self.tmp / "source", self.k.main)
        os.environ["TICKETD_UPDATE_URL"] = self.src.url
        os.environ["TICKETD_UPDATE_CHANNEL"] = "stable"

    def trust(self):
        return update.trust(self.con, signing.pub(self.k.main), signing.fp(self.k.main),
                            signing.pub(self.k.backup), signing.fp(self.k.backup))

    def codes(self):
        return [r["code"] for r in self.con.execute("SELECT code FROM events WHERE kind='update' ORDER BY id")]


class EnableTest(UpdateBase):
    def test_default_install_is_disabled_and_sends_nothing(self):
        os.environ.pop("TICKETD_UPDATE_URL")
        self.assertEqual(update.disabled_reasons(), ["没有配置更新源（TICKETD_UPDATE_URL）"])
        out = "\n".join(update.status_lines(self.con))
        self.assertIn("未启用", out)
        self.assertIn("TICKETD_UPDATE_URL", out)
        self.assertTrue(update.doctor_line(self.con).startswith("ℹ️"))
        # 守护进程里的更新器：转很多圈也不发请求
        with mock.patch.object(update, "fetch") as f, mock.patch.object(update.urllib.request, "urlopen") as u:
            up = update.Updater(self.con, clock=lambda: time.time() + 10 ** 6)
            for _ in range(50):
                up.tick()
            f.assert_not_called()
            u.assert_not_called()

    def test_repo_has_no_default_update_url(self):
        self.assertEqual(config._env.__defaults__, None)
        os.environ.pop("TICKETD_UPDATE_URL")
        self.assertEqual(config.update_url(), "")
        example = (config.REPO / "config.example.env").read_text(encoding="utf-8")
        for line in example.splitlines():
            if line.startswith("TICKETD_UPDATE_URL"):
                self.assertEqual(line.split("=", 1)[1].strip(), "")

    def test_url_without_keys_is_not_enabled(self):
        with mock.patch.object(update, "is_managed", return_value=True):
            why = update.disabled_reasons()
            self.assertTrue(any("没有信任的公钥" in w for w in why), why)
            line = update.doctor_line(self.con)
        self.assertTrue(line.startswith("❌"), line)
        self.assertIn("没有信任的公钥", line)

    def test_unmanaged_is_not_enabled(self):
        self.trust()
        why = update.disabled_reasons()
        self.assertTrue(any("受管安装" in w for w in why), why)

    def test_enabled_when_everything_is_there(self):
        self.trust()
        with mock.patch.object(update, "is_managed", return_value=True):
            self.assertEqual(update.disabled_reasons(), [])
            self.assertTrue(update.doctor_line(self.con).startswith("✅"))

    def test_ssh_keygen_without_verify_refuses(self):
        self.trust()
        fake = self.tmp / "old-ssh-keygen"
        fake.write_text("#!/bin/sh\necho 'unknown option -- Y' >&2\nexit 1\n")
        fake.chmod(0o755)
        with mock.patch.object(update, "is_managed", return_value=True), \
                mock.patch.object(updsig, "ssh_keygen", return_value=str(fake)):
            why = update.disabled_reasons()
            self.assertTrue(any("-Y verify" in w for w in why), why)
            self.assertIn("-Y verify", update.doctor_line(self.con))
        self.assertTrue(updsig.verify_available())  # 真的 ssh-keygen 可以

    def test_http_scheme_rejected(self):
        os.environ["TICKETD_UPDATE_URL"] = "http://example.com/u"
        self.assertTrue(any("https" in w for w in update.disabled_reasons()))

    def test_config_floor_and_defaults(self):
        os.environ["TICKETD_UPDATE_EVERY"] = "60"
        self.assertEqual(config.update_every(), 600)
        os.environ.pop("TICKETD_UPDATE_EVERY")
        self.assertEqual((config.update_every(), config.update_probation(), config.update_stale_days()),
                         (3600, 600, 30.0))
        os.environ["TICKETD_UPDATE_CHANNEL"] = "nightly"
        self.assertEqual(config.update_channel(), "stable")


class TrustTest(UpdateBase):
    def test_fingerprints_match(self):
        out = self.trust()
        self.assertIn(signing.fp(self.k.main), out[0])
        self.assertEqual(updsig.signer_key(update.main_signers()), " ".join(signing.pub(self.k.main).split()[:2]))
        self.assertEqual(oct(update.main_signers().stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(update.backup_signers().stat().st_mode & 0o777), "0o600")

    def test_main_fingerprint_mismatch_writes_nothing(self):
        with self.assertRaises(update.UpdateError) as cm:
            update.trust(self.con, signing.pub(self.k.evil), signing.fp(self.k.main),
                         signing.pub(self.k.backup), signing.fp(self.k.backup))
        self.assertIn("指纹不一致", str(cm.exception))
        self.assertFalse(update.main_signers().exists())
        self.assertFalse(update.backup_signers().exists())

    def test_backup_mismatch_keeps_existing_trust(self):
        self.trust()
        before = (update.main_signers().read_bytes(), update.backup_signers().read_bytes())
        with self.assertRaises(update.UpdateError):
            update.trust(self.con, signing.pub(self.k.main2), signing.fp(self.k.main2),
                         signing.pub(self.k.evil), signing.fp(self.k.backup))
        self.assertEqual((update.main_signers().read_bytes(), update.backup_signers().read_bytes()), before)

    def test_rejects_rsa_same_key_and_bad_fingerprint_format(self):
        with self.assertRaises(update.UpdateError):
            update.trust(self.con, signing.pub(self.k.rsa), "SHA256:" + "A" * 43,
                         signing.pub(self.k.backup), signing.fp(self.k.backup))
        with self.assertRaises(update.UpdateError):
            update.trust(self.con, signing.pub(self.k.main), signing.fp(self.k.main),
                         signing.pub(self.k.main), signing.fp(self.k.main))
        with self.assertRaises(update.UpdateError):
            update.trust(self.con, signing.pub(self.k.main), "abc", signing.pub(self.k.backup), signing.fp(self.k.backup))
        self.assertFalse(update.main_signers().exists())

    def test_key_from_file_path(self):
        update.trust(self.con, str(self.k.main) + ".pub", signing.fp(self.k.main),
                     str(self.k.backup) + ".pub", signing.fp(self.k.backup))
        self.assertTrue(update.main_signers().is_file())

    def test_worker_cannot_change_trust_or_settings(self):
        os.environ["TICKETD_TICKET"] = "T0001"
        for action in ("trust", "hold", "resume", "rollback", "adopt"):
            with self.assertRaises(PermissionError) as cm:
                update.command(self.con, args(action, main=signing.pub(self.k.main), fingerprint=signing.fp(self.k.main),
                                              backup=signing.pub(self.k.backup),
                                              backup_fingerprint=signing.fp(self.k.backup)))
            self.assertIn("用户本人", str(cm.exception))
        self.assertFalse(update.main_signers().exists())
        os.environ.pop("TICKETD_TICKET")
        os.environ["TICKETD_SPOOL"] = str(self.tmp / "x" / ".reports")
        with self.assertRaises(PermissionError):
            update.command(self.con, args("resume"))
        # 看状态可以
        self.assertIn("自动更新", update.command(self.con, args("status")))

    def test_worker_sandbox_cannot_write_trust_files(self):
        """worker 沙箱的可写范围（perms.py）不含 ~/.ticketd/update。"""
        from ticketd import perms
        src = Path(perms.__file__).read_text(encoding="utf-8")
        self.assertNotIn("update_dir", src)
        self.assertTrue(str(update.update_dir()).startswith(str(config.home())))


class CheckTest(UpdateBase):
    def setUp(self):
        super().setUp()
        self.trust()

    def check(self, version="0.3.0", now=None):
        return update.check(self.con, version=version, now=now)

    def test_normal_upgrade_downloads_and_records(self):
        self.src.manifest("0.3.1", seq=5)
        out = self.check()
        self.assertIn("已下载", out)
        pend = update.kv_json(self.con, "update.pending")
        self.assertEqual((pend["version"], pend["seq"]), ("0.3.1", 5))
        self.assertTrue((update.update_dir() / "pending" / "larkyard-0.3.1.tar.gz").is_file())
        self.assertEqual(update.max_seq(self.con), 5)
        self.assertEqual(self.codes(), ["downloaded"])
        # 再查一次：同一份清单不算重放，也不重复下载
        self.assertIn("待安装", self.check())
        self.assertEqual(self.codes(), ["downloaded"])

    def test_untrusted_signature(self):
        self.src.manifest("0.3.1", seq=5, key=self.k.evil)
        self.assertIn("验签失败", self.check())
        self.assertEqual(self.codes(), ["bad_signature"])
        self.assertIsNone(update.kv_json(self.con, "update.pending"))
        self.assertEqual(update.max_seq(self.con), 0)

    def test_backup_key_cannot_sign_releases(self):
        self.src.manifest("0.3.1", seq=5, key=self.k.backup)
        self.assertIn("验签失败", self.check())

    def test_signature_from_other_namespace(self):
        doc = self.src.manifest("0.3.1", seq=5)
        p = self.src.root / "channels" / "stable" / "manifest.json"
        import subprocess
        q = self.tmp / "m.json"
        q.write_bytes(p.read_bytes())
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(self.k.main), "-n", "git", str(q)], check=True,
                       capture_output=True)
        Path(str(p) + ".sig").write_bytes(Path(str(q) + ".sig").read_bytes())
        self.assertIn("验签失败", self.check())

    def test_tampered_manifest_bytes(self):
        self.src.manifest("0.3.1", seq=5)
        p = self.src.root / "channels" / "stable" / "manifest.json"
        p.write_bytes(p.read_bytes().replace(b'"seq":5', b'"seq":9'))
        self.assertIn("验签失败", self.check())

    def test_package_swapped(self):
        self.src.manifest("0.3.1", seq=5)
        (self.src.root / "packages" / "larkyard-0.3.1.tar.gz").write_bytes(signing.package("0.3.1", {"x": "evil"}))
        out = self.check()
        self.assertIn("校验失败", out)
        self.assertEqual(self.codes(), ["bad_hash"])
        self.assertEqual(list((update.update_dir() / "pending").iterdir()), [])
        self.assertIsNone(update.kv_json(self.con, "update.pending"))

    def test_package_larger_than_manifest(self):
        self.src.manifest("0.3.1", seq=5)
        p = self.src.root / "packages" / "larkyard-0.3.1.tar.gz"
        p.write_bytes(p.read_bytes() + b"x" * 100)
        self.assertIn("校验失败", self.check())

    def test_canary_manifest_on_stable(self):
        self.src.manifest("0.3.1", seq=5, channel="canary", at="channels/stable/manifest.json")
        self.assertIn("通道不符", self.check())
        self.assertEqual(self.codes(), ["wrong_channel"])

    def test_wrong_product(self):
        self.src.manifest("0.3.1", seq=5, product="other")
        self.assertIn("格式", self.check())

    def test_replay_older_seq(self):
        metrics.kv_set(self.con, "update.max_seq", "42")
        self.src.manifest("0.3.1", seq=40)
        self.assertIn("旧清单", self.check())
        self.assertEqual(self.codes(), ["stale"])

    def test_replay_equal_seq_different_content(self):
        self.src.manifest("0.3.1", seq=5)
        self.check()
        self.src.manifest("0.3.2", seq=5)
        self.assertIn("旧清单", self.check())

    def test_max_seq_survives_and_never_decreases(self):
        self.src.manifest("0.3.1", seq=50)
        self.check()
        update._raise_seq(self.con, 10)
        self.assertEqual(update.max_seq(self.con), 50)

    def test_release_pointing_to_older_version(self):
        self.src.manifest("0.2.1", seq=5)
        self.assertIn("拒绝降级", self.check())
        self.assertEqual(self.codes(), ["downgrade"])

    def test_rollback_within_validity(self):
        self.src.manifest("0.2.1", seq=6, kind="rollback", expires_at=updsig.iso(time.time() + 86400))
        self.assertIn("已下载", self.check())
        self.assertEqual(update.kv_json(self.con, "update.pending")["kind"], "rollback")

    def test_rollback_expired(self):
        self.src.manifest("0.2.1", seq=6, kind="rollback", expires_at=updsig.iso(time.time() - 60))
        self.assertIn("回滚已过期", self.check())
        self.assertEqual(self.codes(), ["rollback_expired"])

    def test_rollback_without_expiry_is_bad(self):
        self.src.manifest("0.2.1", seq=6, kind="rollback")
        self.assertIn("格式", self.check())

    def test_rollback_replayed_after_expiry(self):
        self.src.manifest("0.2.1", seq=6, kind="rollback", expires_at=updsig.iso(time.time() + 3600))
        self.check()
        update._drop_pending(self.con)
        # 同一份回滚清单，过期后再被发布（或一直留着）：不装
        self.assertNotIn("已下载", self.check(now=time.time() + 7200))

    def test_rollback_expires_before_install(self):
        self.src.manifest("0.2.1", seq=6, kind="rollback", expires_at=updsig.iso(time.time() + 60))
        self.check()
        pend = update.kv_json(self.con, "update.pending")
        with self.assertRaises(update.UpdateError):
            update.install(self.con, pend, now=time.time() + 120)
        self.assertIn("rollback_expired", self.codes())

    def test_too_far_jump(self):
        self.src.manifest("0.5.0", seq=5, min_from="0.3.0")
        out = self.check(version="0.2.0")
        self.assertIn("手工升级到 0.3.0", out)
        self.assertIn("手工升级到 0.3.0", "\n".join(update.status_lines(self.con)))
        self.assertIsNone(update.kv_json(self.con, "update.pending"))

    def test_same_version_is_current(self):
        self.src.manifest("0.3.0", seq=5)
        self.assertIn("已是最新", self.check())
        self.assertEqual(update.max_seq(self.con), 5)

    def test_blocked_version_not_installed_again(self):
        update.block_version(self.con, "0.3.1")
        self.src.manifest("0.3.1", seq=5)
        self.assertIn("拉黑", self.check())
        self.src.manifest("0.3.2", seq=6)
        self.assertIn("已下载", self.check())

    def test_unsigned_extra_pubkey_in_source_is_ignored(self):
        before = update.main_signers().read_bytes()
        self.src.root.mkdir(parents=True, exist_ok=True)
        (self.src.root / "main.allowed_signers").write_text("larkyard-release %s\n" % signing.pub(self.k.evil))
        (self.src.root / "keys").mkdir()
        (self.src.root / "keys" / "main.pub").write_text(signing.pub(self.k.evil))
        self.src.manifest("0.3.1", seq=5, key=self.k.evil)
        self.check()
        self.assertEqual(update.main_signers().read_bytes(), before)

    def test_unreachable_source_only_records(self):
        os.environ["TICKETD_UPDATE_URL"] = (self.tmp / "nope").as_uri()
        out = self.check()
        self.assertIn("没有发布的清单", out)
        with mock.patch.object(update, "fetch", side_effect=update.FetchError("timed out")):
            out = self.check()
        self.assertIn("检查失败", out)
        self.assertEqual(self.codes(), ["check_failed"])
        st = "\n".join(update.status_lines(self.con))
        self.assertIn("timed out", st)
        self.assertIn("上次成功", st)
        with mock.patch.object(update, "fetch", side_effect=update.FetchError("timed out")):
            self.check()
        self.assertEqual(self.codes(), ["check_failed"])  # 一天只记一条

    def test_https_404_is_absent(self):
        import urllib.error
        err = urllib.error.HTTPError("https://x/y", 404, "nf", {}, None)
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=err):
            self.assertIsNone(update.fetch("https://x/y"))
        err = urllib.error.HTTPError("https://x/y", 503, "busy", {}, None)
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(update.FetchError):
                update.fetch("https://x/y")

    def test_jitter_range(self):
        import random
        rnd = random.Random(1)
        delays = [update.next_delay(3600, rand=rnd.uniform) for _ in range(100)]
        self.assertTrue(all(3600 <= d <= 4200 for d in delays))
        self.assertGreater(max(delays) - min(delays), 300)


class RekeyTest(UpdateBase):
    def setUp(self):
        super().setUp()
        self.trust()

    def test_backup_signed_rekey_replaces_main(self):
        self.src.rekey(100, self.k.main2, self.k.backup)
        self.src.manifest("0.3.1", seq=101, key=self.k.main)  # 旧主私钥签的
        out = update.check(self.con, version="0.3.0")
        self.assertIn("验签失败", out)
        self.assertEqual(updsig.signer_key(update.main_signers()), " ".join(signing.pub(self.k.main2).split()[:2]))
        self.assertIn("rekey", self.codes())
        self.src.manifest("0.3.1", seq=102, key=self.k.main2)
        self.assertIn("已下载", update.check(self.con, version="0.3.0"))

    def test_main_signed_rekey_is_ignored(self):
        before = update.main_signers().read_bytes()
        self.src.rekey(100, self.k.evil, self.k.main)
        update.check(self.con, version="0.3.0")
        self.assertEqual(update.main_signers().read_bytes(), before)
        self.assertIn("rekey_invalid", self.codes())

    def test_rekey_resets_seq_inflated_by_leaked_key(self):
        self.src.manifest("0.3.1", seq=10 ** 9)  # 泄露的主私钥签了个序号很大的清单
        update.check(self.con, version="0.3.0")
        self.assertEqual(update.max_seq(self.con), 10 ** 9)
        self.src.rekey(200, self.k.main2, self.k.backup)
        (self.src.root / "channels" / "stable" / "manifest.json").unlink()
        update.check(self.con, version="0.3.0")
        self.assertEqual(update.max_seq(self.con), 200)
        self.assertIsNone(update.kv_json(self.con, "update.pending"))  # 旧主公钥验过的待装包被丢弃
        self.src.manifest("0.3.2", seq=201, key=self.k.main2)
        self.assertIn("已下载", update.check(self.con, version="0.3.0"))

    def test_replayed_old_rekey_cannot_bring_back_retired_key(self):
        """换钥清单只替换它点名的旧主公钥。本机已经信任 main2：重放早先 main → main2 的那份当作已换过；
        一份点名 evil 为旧主公钥、要换回 main 的清单（备份私钥签的也一样）不生效。"""
        update.trust(self.con, signing.pub(self.k.main2), signing.fp(self.k.main2),
                     signing.pub(self.k.backup), signing.fp(self.k.backup))
        self.src.rekey(100, self.k.main2, self.k.backup, old_main=self.k.main)  # 早先 main → main2 的那份
        update.check(self.con, version="0.3.0")
        self.assertEqual(updsig.signer_key(update.main_signers()), " ".join(signing.pub(self.k.main2).split()[:2]))
        self.assertNotIn("rekey_invalid", self.codes())  # 新主公钥就是本机信任的：当作已换过
        self.src.rekey(300, self.k.main, self.k.backup, old_main=self.k.evil)  # 点名的旧主公钥不是本机的
        update.check(self.con, version="0.3.0")
        self.assertEqual(updsig.signer_key(update.main_signers()), " ".join(signing.pub(self.k.main2).split()[:2]))
        self.assertIn("rekey_invalid", self.codes())

    def test_rekey_applied_once(self):
        self.src.rekey(100, self.k.main2, self.k.backup)
        update.check(self.con, version="0.3.0")
        self.src.manifest("0.3.1", seq=150, key=self.k.main2)
        update.check(self.con, version="0.3.0")
        self.assertEqual(update.max_seq(self.con), 150)  # 留着的换钥清单不会把序号拉回 100
        self.assertEqual(self.codes().count("rekey"), 1)

    def test_interrupted_rekey_is_finished_next_check(self):
        """公钥写好了、序号还没重置就进程退出：下次检查补完重置，不会卡在被泄露私钥推高的序号上。"""
        self.src.manifest("0.3.1", seq=10 ** 9)
        update.check(self.con, version="0.3.0")
        self.src.rekey(200, self.k.main2, self.k.backup)
        (self.src.root / "channels" / "stable" / "manifest.json").unlink()
        with mock.patch.object(update, "_finish_rekey", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                update.check(self.con, version="0.3.0")
        self.assertEqual(update.max_seq(self.con), 10 ** 9)
        update.check(self.con, version="0.3.0")
        self.assertEqual(update.max_seq(self.con), 200)
        self.src.manifest("0.3.2", seq=201, key=self.k.main2)
        self.assertIn("已下载", update.check(self.con, version="0.3.0"))


class PauseTest(UpdateBase):
    def setUp(self):
        super().setUp()
        self.trust()

    def test_signed_pause_in_window_blocks_install(self):
        now = time.time()
        self.src.pause(3, now - 3600, now + 86400)
        self.src.manifest("0.3.1", seq=5)
        update.check(self.con, version="0.3.0", now=now)
        self.assertIsNotNone(update.pause_until(self.con, now))
        self.assertIn("更新源已暂停", "\n".join(update.status_lines(self.con, now)))
        up = update.Updater(self.con)
        self.assertFalse(up._maybe_install(None, None, now))

    def test_expired_pause_is_ignored(self):
        now = time.time()
        self.src.pause(3, now - 8 * 86400, now - 86400)
        update.check(self.con, version="0.3.0", now=now)
        self.assertIsNone(update.pause_until(self.con, now))

    def test_unsigned_or_wrongly_signed_pause_ignored(self):
        now = time.time()
        self.src.pause(3, now - 3600, now + 86400, key=self.k.evil)
        update.check(self.con, version="0.3.0", now=now)
        self.assertIsNone(update.pause_until(self.con, now))
        self.assertIn("pause_invalid", self.codes())

    def test_unpublished_pause_clears(self):
        now = time.time()
        self.src.pause(3, now - 3600, now + 86400)
        update.check(self.con, version="0.3.0", now=now)
        (self.src.root / "pause.json").unlink()
        update.check(self.con, version="0.3.0", now=now)
        self.assertIsNone(update.pause_until(self.con, now))

    def test_local_hold_and_resume(self):
        now = time.time()
        out = update.command(self.con, args("hold", days=3))
        self.assertIn("已暂停", out)
        self.assertIsNotNone(update.hold_until(self.con, now))
        self.assertIsNone(update.hold_until(self.con, now + 3 * 86400 + 60))  # 到期自动恢复
        with self.assertRaises(update.UpdateError):
            update.command(self.con, args("hold", days=8))
        update.command(self.con, args("resume"))
        self.assertIsNone(update.hold_until(self.con, now))

    def test_hold_allows_download_but_not_install(self):
        now = time.time()
        update.command(self.con, args("hold", days=3))
        self.src.manifest("0.3.1", seq=5)
        self.assertIn("已下载", update.check(self.con, version="0.3.0", now=now))
        self.assertFalse(update.Updater(self.con)._maybe_install(None, None, now))


if __name__ == "__main__":
    unittest.main()
