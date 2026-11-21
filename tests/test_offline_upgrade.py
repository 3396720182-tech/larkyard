"""离线升级包（release.py bundle + scripts/upgrade-offline.sh + install.sh --from-dir）：升级包放在任何路径
（空格、中文、# % ?）都能核对、检查和安装。用测试钥匙签的发布目录，临时 HOME；不碰服务管理器。"""
from __future__ import annotations

import io
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from helpers import REPO, TicketdTestCase
from signing import Source, fp, keygen

sys.path.insert(0, str(REPO / "scripts"))
import release  # noqa: E402

from ticketd import update  # noqa: E402

VERSION = "9.9.1"
# 故意刁钻的目录名：空格、中文、URL 里有特殊含义的 # % ?
ODD = ("下载 含空格", "a#b %20c?d", "升级 包（副本）")

OLD_CONFIG = '''import os
from pathlib import Path
def _file(name):
    p = Path(os.environ.get("TICKETD_HOME", str(Path.home() / ".ticketd"))) / "config.env"
    for line in p.read_text().splitlines() if p.exists() else []:
        if line.startswith(name + "="):
            return line.split("=", 1)[1]
    return None
def workroot():
    return Path(os.environ.get("TICKETD_WORKROOT") or _file("TICKETD_WORKROOT") or str(Path.home() / "larkyard-tickets"))
def owner():
    return os.environ.get("TICKETD_OWNER") or _file("TICKETD_OWNER") or ""
'''


def real_package(version: str) -> bytes:
    """包里放真的 ticketd/ 代码（install.sh 要用包里的代码跑 ticketd.migrate --check 和 ticketd.bootstrap）。
    不放 vendor/runtime.json：bootstrap 就不下载 Node / lark-cli。"""
    files = {}
    for f in sorted((REPO / "ticketd").rglob("*.py")):
        files["ticketd/" + f.relative_to(REPO / "ticketd").as_posix()] = f.read_bytes()
    files["ticketd/__init__.py"] = ('__version__ = "%s"\n' % version).encode()
    for n in ("ticket", "ticketd"):
        files["bin/" + n] = (REPO / "bin" / n).read_bytes()
    files["config.example.env"] = b"TICKETD_OWNER=\n"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        for name, data in sorted(files.items()):
            ti = tarfile.TarInfo("larkyard-%s/%s" % (version, name))
            ti.size, ti.mtime, ti.mode = len(data), 0, 0o755 if name.startswith("bin/") else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return raw.getvalue()


class OfflineBase(TicketdTestCase):
    def setUp(self):
        super().setUp()
        keys = self.tmp / "keys"
        keys.mkdir()
        self.main, self.backup = keygen(keys, "main"), keygen(keys, "backup")
        self.feed = self.tmp / "release home" / "feed"
        src = Source(self.feed, self.main)
        src.add_package(VERSION, real_package(VERSION))
        src.manifest(VERSION, 5)
        (self.feed / "keys").mkdir(parents=True)
        shutil.copy2(str(self.main) + ".pub", str(self.feed / "keys/main.pub"))
        shutil.copy2(str(self.backup) + ".pub", str(self.feed / "keys/backup.pub"))
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        text = re.sub(r'^RELEASE_KEY_FP="[^"]*"', 'RELEASE_KEY_FP="%s"' % fp(self.main), text, flags=re.M)
        text = re.sub(r'^BACKUP_KEY_FP="[^"]*"', 'BACKUP_KEY_FP="%s"' % fp(self.backup), text, flags=re.M)
        text = text.replace('RELEASE_URL="@RELEASE_URL@"', 'RELEASE_URL="https://updates.example.invalid/ly"')
        (self.feed / "install.sh").write_text(text, encoding="utf-8")
        self.user = self.tmp / "user home"   # HOME 也带空格
        self.user.mkdir()
        self.th = self.user / ".ticketd"

    def bundle(self, out: Path, *extra) -> list:
        out.mkdir(parents=True, exist_ok=True)
        return release.COMMANDS["bundle"](release.build_parser().parse_args(
            ["bundle", "--feed", str(self.feed), "--out", str(out)] + list(extra)))

    def unzip_to(self, where: Path) -> Path:
        """像访达那样解压（保留 unix 权限位）到 where 下，返回升级包目录。"""
        z = self.tmp / "out" / ("larkyard-%s-upgrade.zip" % VERSION)
        if not z.exists():
            self.bundle(self.tmp / "out")
        where.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(z)) as zf:
            for zi in zf.infolist():
                dest = where / zi.filename
                if zi.filename.endswith("/"):
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(zi))
                os.chmod(str(dest), (zi.external_attr >> 16) & 0o777)
        return where / ("larkyard-%s-upgrade" % VERSION)

    def env(self, **kw) -> dict:
        tmpdir = self.tmp / "tmp dir 临时"
        tmpdir.mkdir(exist_ok=True)
        e = {"HOME": str(self.user), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
             "TMPDIR": str(tmpdir), "TICKETD_PYTHON": sys.executable}
        e.update(kw)
        return e

    def run_sh(self, script: Path, *args, **env) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/sh", str(script)] + list(args), env=self.env(**env), capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL)


class BundleTest(OfflineBase):
    def test_layout_modes_and_zip(self):
        out = self.tmp / "out 输出"
        lines = self.bundle(out, "--readme", str(REPO / "README.md"))
        d = out / ("larkyard-%s-upgrade" % VERSION)
        got = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
        self.assertEqual(got, sorted([
            "README.md", "upgrade.sh", "feed/install.sh", "feed/CHANNEL", "feed/keys/main.pub",
            "feed/keys/backup.pub", "feed/channels/stable/manifest.json", "feed/channels/stable/manifest.json.sig",
            "feed/packages/larkyard-%s.tar.gz" % VERSION]))
        self.assertEqual((d / "upgrade.sh").read_bytes(), (REPO / "scripts/upgrade-offline.sh").read_bytes())
        for f in ("upgrade.sh", "feed/install.sh"):
            self.assertTrue(os.stat(str(d / f)).st_mode & stat.S_IXUSR, f)
        self.assertEqual((d / "feed/install.sh").read_bytes(), (self.feed / "install.sh").read_bytes())
        self.assertEqual((d / "feed/CHANNEL").read_text(), "stable\n")
        self.assertEqual(sorted(x.name for x in out.iterdir()), sorted([d.name, d.name + ".zip"]))  # 没有留下临时文件
        z = out / ("larkyard-%s-upgrade.zip" % VERSION)
        with zipfile.ZipFile(str(z)) as zf:
            names = zf.namelist()
            self.assertTrue(all(n.startswith("larkyard-%s-upgrade/" % VERSION) for n in names))
            modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in zf.infolist()}
        self.assertEqual(modes["larkyard-%s-upgrade/upgrade.sh" % VERSION], 0o755)
        self.assertEqual(modes["larkyard-%s-upgrade/feed/keys/main.pub" % VERSION], 0o644)
        self.assertTrue(any(str(z) in x for x in lines))
        with self.assertRaises(release.ReleaseError):  # 不覆盖
            self.bundle(out)

    def test_refuses_inconsistent_feed(self):
        pkg = self.feed / "packages" / ("larkyard-%s.tar.gz" % VERSION)
        good = pkg.read_bytes()
        pkg.write_bytes(good + b"x")
        with self.assertRaisesRegex(release.ReleaseError, "摘要"):
            self.bundle(self.tmp / "o1")
        pkg.write_bytes(good)
        other = keygen(self.tmp / "keys", "other")
        shutil.copy2(str(other) + ".pub", str(self.feed / "keys/main.pub"))
        with self.assertRaisesRegex(release.ReleaseError, "签名"):
            self.bundle(self.tmp / "o2")
        shutil.copy2(str(self.main) + ".pub", str(self.feed / "keys/main.pub"))
        text = (self.feed / "install.sh").read_text(encoding="utf-8")
        (self.feed / "install.sh").write_text(re.sub(r'^RELEASE_URL="[^"]*"', 'RELEASE_URL="@RELEASE_URL@"', text, flags=re.M),
                                              encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseError, "发布地址"):
            self.bundle(self.tmp / "o3")
        (self.feed / "install.sh").write_text(text, encoding="utf-8")
        bad = self.tmp / "upgrade.sh"
        bad.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseError, "说明文件不能叫"):
            self.bundle(self.tmp / "o4", "--readme", str(bad))
        with self.assertRaises(OSError):  # 说明文件不存在：失败，不留下临时目录
            self.bundle(self.tmp / "o5", "--readme", str(self.tmp / "没有这个.txt"))
        for n in ("o1", "o2", "o3", "o4", "o5"):
            self.assertEqual(list((self.tmp / n).iterdir()), [], n)

    def test_channel_is_carried_into_the_package(self):
        src = Source(self.feed, self.main)
        src.packages[VERSION] = (self.feed / "packages" / ("larkyard-%s.tar.gz" % VERSION)).read_bytes()
        src.manifest(VERSION, 6, channel="early")
        out = self.tmp / "early"
        self.bundle(out, "--channel", "early")
        d = out / ("larkyard-%s-upgrade" % VERSION)
        self.assertFalse((d / "feed/channels/stable").exists())
        p = self.run_sh(d / "upgrade.sh", "--dry-run")
        self.assertIn("取 early 通道的清单并验签", p.stderr)
        self.assertIn("检查现有安装", p.stderr)


class AnyPathTest(OfflineBase):
    def test_dry_run_from_odd_paths(self):
        # 没有在跑的旧服务：核对、验签、解包、只读检查都要走到，停在「没找到跑 ticketd 的服务」
        for name in ODD:
            with self.subTest(name=name):
                d = self.unzip_to(self.tmp / name)
                p = self.run_sh(d / "upgrade.sh", "--dry-run")
                out = p.stdout + p.stderr
                self.assertEqual(p.returncode, 1, out)
                for want in ("用本地发布目录 %s/feed" % d.resolve(), "取 stable 通道的清单并验签",
                             "下载 Larkyard %s" % VERSION, "检查现有安装", "没找到跑 ticketd 的服务"):
                    self.assertIn(want, out)
                self.assertFalse(self.th.exists())

    def test_dry_run_passes_with_old_clone_service(self):
        # 在跑的旧克隆（代码目录本身也带空格和中文）：只读检查通过，计划里打出旧代码目录
        repo = self.user / "旧 代码 #1"
        (repo / "bin").mkdir(parents=True)
        (repo / "ticketd").mkdir()
        (repo / "bin/ticketd").write_text("#!/bin/sh\n", encoding="utf-8")
        (repo / "ticketd/__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
        (repo / "ticketd/config.py").write_text(OLD_CONFIG, encoding="utf-8")
        self.th.mkdir()
        (self.th / "config.env").write_text("TICKETD_OWNER=ou_test_owner\n", encoding="utf-8")
        saved = os.environ.get("HOME")
        os.environ["HOME"] = str(self.user)
        try:
            sf = update.service_file(update.DEFAULT_LABEL)
        finally:
            os.environ["HOME"] = saved
        sf.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            sf.write_bytes(plistlib.dumps({"Label": update.DEFAULT_LABEL, "ProgramArguments": [str(repo / "bin/ticketd")],
                                           "EnvironmentVariables": {"LARK_CHANNEL_PROFILE": "claude"}}))
        else:
            sf.write_text('[Service]\nExecStart="%s/bin/ticketd"\nEnvironment="LARK_CHANNEL_PROFILE=claude"\n' % repo,
                          encoding="utf-8")
        def files():  # macOS 自带的 python3 会往 ~/Library/Caches/com.apple.python 写字节码缓存：不算
            return sorted(str(p) for p in self.user.rglob("*") if "Library/Caches" not in str(p))
        before = files()
        d = self.unzip_to(self.tmp / ODD[1])
        p = self.run_sh(d / "upgrade.sh", "--dry-run", XDG_CONFIG_HOME=str(self.user / ".config"))
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("在直接跑 %s" % repo, out)
        self.assertIn("版本：0.2.0", out)
        self.assertIn("--dry-run：核对和检查都通过了，没有改任何东西", out)
        self.assertEqual(files(), before)  # 只读

    def test_install_from_odd_path(self):
        # 不是升级（--no-setup 的一行安装）：同一个本地发布目录真的装成受管安装，地址按 RFC 3986 转义写进更新源
        d = self.unzip_to(self.tmp / ODD[1])
        p = self.run_sh(d / "feed/install.sh", "--from-dir", str(d / "feed"), "--no-setup")
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(os.readlink(str(self.th / "app/current")), "versions/%s" % VERSION)
        cfg = (self.th / "config.env").read_text(encoding="utf-8")
        self.assertIn("TICKETD_UPDATE_URL=%s" % (d / "feed").resolve().as_uri(), cfg)
        self.assertIn("a%23b%20%2520c%3Fd", cfg)

    def test_from_and_from_dir_together_refused(self):
        p = self.run_sh(self.feed / "install.sh", "--from", "https://x.example", "--from-dir", str(self.feed))
        self.assertEqual(p.returncode, 1)
        self.assertIn("只能用一个", p.stderr)
        p = self.run_sh(self.feed / "install.sh", "--from-dir", str(self.tmp / "不存在 的目录"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("本地发布目录不存在", p.stderr)

    def test_no_arguments_reach_install_sh(self):
        # 正式升级那条命令不带参数：macOS 的 /bin/sh（bash 3.2）+ set -u 下也要把「没有参数」正确转过去
        d = self.unzip_to(self.tmp / ODD[0])
        (d / "feed/install.sh").write_text('#!/bin/sh\nfor a in "$@"; do printf "[%s]" "$a"; done\n', encoding="utf-8")
        p = self.run_sh(d / "upgrade.sh")
        self.assertEqual(p.returncode, 0, p.stderr)
        head = "[--from-dir][%s/feed][--channel][stable][--upgrade-existing]" % d.resolve()
        self.assertEqual(p.stdout, head)
        p = self.run_sh(d / "upgrade.sh", "--dry-run", "a b")
        self.assertEqual(p.stdout, head + "[--dry-run][a b]")

    def test_planted_module_in_cwd_is_not_imported(self):
        # 在「下载」里执行：那里有个 pathlib.py / json.py 也顶替不了标准库（内联 Python 都带 -I）
        d = self.unzip_to(self.tmp / ODD[0])
        cwd = self.tmp / "downloads 下载"
        cwd.mkdir()
        for mod in ("pathlib", "json", "hashlib"):
            (cwd / ("%s.py" % mod)).write_text("raise SystemExit('planted %s')\n" % mod, encoding="utf-8")
        p = subprocess.run(["/bin/sh", str(d / "upgrade.sh"), "--dry-run"], cwd=str(cwd), env=self.env(),
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertNotIn("planted", p.stdout + p.stderr)
        self.assertIn("检查现有安装", p.stderr)
