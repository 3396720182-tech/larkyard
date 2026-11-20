"""一行安装的第二段（ticketd.bootstrap）：核对、装成受管安装、可重复执行、不降级、失败不留痕。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from helpers import TicketdTestCase
from signing import Source, fp, keygen, package, pub

from ticketd import bootstrap, bridge, config, update, updsig


class BootstrapTest(TicketdTestCase):
    def setUp(self):
        super().setUp()
        os.environ["HOME"] = str(self.tmp / "user")
        keys = Path(tempfile.mkdtemp(dir=str(self.tmp)))
        self.main, self.backup, self.other = keygen(keys, "main"), keygen(keys, "backup"), keygen(keys, "other")
        self.src = Source(self.tmp / "src", self.main)
        self.blob = b"#!/bin/sh\necho node\n"
        import io
        import tarfile
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tf:
            for name in ("node-v24.0.1-%s/bin/node" % bridge.platform_key(), "lark-cli"):
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = len(self.blob), 0o755
                tf.addfile(ti, io.BytesIO(self.blob))
        self.tgz = raw.getvalue()
        sha = hashlib.sha256(self.tgz).hexdigest()
        self.runtime = json.dumps({
            "node": {"version": "24.0.1", "assets": {bridge.platform_key(): {"url": "https://x/n", "sha256": sha}}},
            "lark_cli": {"version": "1.0.86", "assets": {bridge.platform_key(): {"url": "https://x/l", "sha256": sha}}}})
        self.fetched = []

    def fetch(self, urls, dest, sha, **kw):
        self.fetched.append(urls[0])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.tgz)
        return urls[0]

    def publish(self, version, seq):
        entry = "// bridge %s\n" % version
        files = {"vendor/runtime.json": self.runtime, "vendor/bridge/bin/lark-channel-bridge.mjs": entry,
                 "bin/ticket": "#!/bin/sh\n", "config.example.env": "TICKETD_OWNER=\n# TICKETD_UPDATE_URL=\n"}
        self.src.add_package(version, package(version, files))
        doc = self.src.manifest(version, seq)
        m = self.src.root / "channels" / "stable" / "manifest.json"
        pkg = self.src.root / "packages" / ("larkyard-%s.tar.gz" % version)
        return doc, pkg, m.read_bytes(), Path(str(m) + ".sig").read_bytes()

    def install(self, pkg, data, sig, main=None, main_fp=None, channel="stable"):
        return bootstrap.install(pkg, data, sig, pub(main or self.main), main_fp or fp(main or self.main),
                                 pub(self.backup), fp(self.backup), self.src.url, channel, fetch=self.fetch)

    def test_fresh_install_is_managed_and_trusted(self):
        doc, pkg, data, sig = self.publish("0.3.0", 7)
        out = self.install(pkg, data, sig)
        self.assertEqual(update.current_version(), "0.3.0")
        self.assertTrue((update.app_dir() / "launcher.py").is_file())
        self.assertEqual(updsig.signer_key(update.main_signers()), " ".join(pub(self.main).split()[:2]))
        self.assertEqual(update.max_seq(self.con), 7)
        self.assertEqual(config.file_value("TICKETD_UPDATE_URL"), self.src.url)
        self.assertEqual(config.file_value("TICKETD_OWNER"), "")
        self.assertTrue(bridge.node_bin().is_file())
        self.assertTrue(bridge.lark_cli().exists())
        self.assertEqual(bridge.bridge_version_dir(), "0.3.0")
        self.assertEqual(update.read_state()["status"], "confirmed")
        self.assertTrue(any("已安装 Larkyard 0.3.0" in x for x in out))
        # 已接受的就是这份清单：守护进程第一次检查不会把它当成新版本或旧清单
        self.assertEqual(update.kv_json(self.con, "update.accepted", None) or
                         self.con.execute("SELECT value FROM kv WHERE key='update.accepted'").fetchone()["value"],
                         hashlib.sha256(data).hexdigest())

    def test_rerun_skips_and_keeps_config(self):
        doc, pkg, data, sig = self.publish("0.3.0", 7)
        self.install(pkg, data, sig)
        p = config.home() / "config.env"
        p.write_text(p.read_text(encoding="utf-8").replace("TICKETD_OWNER=", "TICKETD_OWNER=ou_me"), encoding="utf-8")
        self.fetched.clear()
        out = self.install(pkg, data, sig)
        self.assertIn("本机已是 0.3.0", out)
        self.assertEqual(self.fetched, [])
        self.assertIn("TICKETD_OWNER=ou_me", p.read_text(encoding="utf-8"))
        self.assertEqual(p.read_text(encoding="utf-8").count("\nTICKETD_UPDATE_URL="), 1)

    def test_no_downgrade(self):
        _, pkg4, d4, s4 = self.publish("0.4.0", 8)
        self.install(pkg4, d4, s4)
        _, pkg3, d3, s3 = self.publish("0.3.0", 9)
        out = self.install(pkg3, d3, s3)
        self.assertEqual(update.current_version(), "0.4.0")
        self.assertTrue(any("不降级" in x for x in out))

    def test_wrong_fingerprint_or_signature_writes_nothing(self):
        doc, pkg, data, sig = self.publish("0.3.0", 7)
        with self.assertRaises(bootstrap.BootstrapError):
            self.install(pkg, data, sig, main_fp=fp(self.other))
        with self.assertRaises(bootstrap.BootstrapError):
            self.install(pkg, data, sig, main=self.other)  # 指纹对得上另一把，但签名不是它签的
        with self.assertRaises(bootstrap.BootstrapError):
            self.install(pkg, data.replace(b'"stable"', b'"early"'), sig)
        with self.assertRaises(bootstrap.BootstrapError):
            self.install(pkg, data, sig, channel="early")
        pkg.write_bytes(pkg.read_bytes() + b"x")
        with self.assertRaises(bootstrap.BootstrapError):
            self.install(pkg, data, sig)
        for d in ("app", "update", "runtime", "bridge", "bin"):
            self.assertFalse((config.home() / d).exists(), d)
        self.assertFalse((config.home() / "config.env").exists())

    def test_managed_machine_is_left_to_the_daemon(self):
        _, pkg3, d3, s3 = self.publish("0.3.0", 7)
        self.install(pkg3, d3, s3)
        _, pkg4, d4, s4 = self.publish("0.4.0", 8)
        out = self.install(pkg4, d4, s4)
        self.assertEqual(update.current_version(), "0.3.0")
        self.assertTrue(any("守护进程" in x for x in out))
        self.assertEqual(update.max_seq(self.con), 7)  # 不替守护进程接受新清单

    def test_rollback_manifest_installs_and_other_channel_not_accepted(self):
        self.src.add_package("0.3.0", package("0.3.0", {"bin/ticket": "#!/bin/sh\n", "vendor/runtime.json": self.runtime,
                                                         "config.example.env": "TICKETD_OWNER=\n"}))
        self.src.manifest("0.3.0", 9, channel="early", kind="rollback", expires_at="2026-01-01T00:00:00Z")
        m = self.src.root / "channels" / "early" / "manifest.json"
        out = self.install(self.src.root / "packages" / "larkyard-0.3.0.tar.gz", m.read_bytes(),
                           Path(str(m) + ".sig").read_bytes(), channel="early")
        self.assertEqual(update.current_version(), "0.3.0")
        self.assertEqual(config.file_value("TICKETD_UPDATE_CHANNEL"), "early")
        self.assertEqual(update.max_seq(self.con), 9)
        p = config.home() / "config.env"
        p.write_text(p.read_text(encoding="utf-8").replace("TICKETD_UPDATE_CHANNEL=early", "TICKETD_UPDATE_CHANNEL=canary"),
                     encoding="utf-8")
        _, pkg, data, sig = self.publish("0.3.0", 12)
        out = self.install(pkg, data, sig)
        self.assertTrue(any("更新通道是 canary" in x for x in out))
        self.assertEqual(update.max_seq(self.con), 9)

    def test_enroll_install_does_not_write_update_url(self):
        doc, pkg, data, sig = self.publish("0.3.0", 7)
        bootstrap.install(pkg, data, sig, pub(self.main), fp(self.main), pub(self.backup), fp(self.backup),
                          self.src.url, "stable", fetch=self.fetch, update_url=False)
        self.assertIsNone(config.file_value("TICKETD_UPDATE_URL"))

    def test_refused_in_worker(self):
        doc, pkg, data, sig = self.publish("0.3.0", 7)
        os.environ["TICKETD_TICKET"] = "T0001"
        with self.assertRaises(bootstrap.BootstrapError):
            self.install(pkg, data, sig)

    def test_main_cli_reports_errors(self):
        doc, pkg, data, sig = self.publish("0.3.0", 7)
        d = self.tmp / "args"
        d.mkdir()
        for name, content in (("m.json", data), ("m.sig", sig)):
            (d / name).write_bytes(content)
        (d / "main.pub").write_text(pub(self.main), encoding="utf-8")
        (d / "backup.pub").write_text(pub(self.backup), encoding="utf-8")
        with mock.patch("ticketd.bridge.fetch_verified", side_effect=self.fetch):
            rc = bootstrap.main(["--package", str(pkg), "--manifest", str(d / "m.json"), "--sig", str(d / "m.sig"),
                                 "--main-key", str(d / "main.pub"), "--backup-key", str(d / "backup.pub"),
                                 "--main-fp", fp(self.other), "--backup-fp", fp(self.backup), "--from", self.src.url])
        self.assertEqual(rc, 1)
