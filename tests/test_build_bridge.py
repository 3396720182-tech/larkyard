"""scripts/build_bridge.py 取源码这一步（T0112：源码在仓库的 bridge/ 里）。pnpm 构建不在单测里跑。"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import REPO

spec = importlib.util.spec_from_file_location("build_bridge", str(REPO / "scripts" / "build_bridge.py"))
build_bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_bridge)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(args),
                          capture_output=True, text=True, check=True).stdout


class ExportSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ticketd-bb-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.repo = self.tmp / "repo"
        (self.repo / "bridge" / "src").mkdir(parents=True)
        (self.repo / "bridge" / "package.json").write_text('{"name": "b", "version": "0.7.1"}\n')
        (self.repo / "bridge" / "src" / "a.ts").write_text("export const a = 1;\n")
        (self.repo / "README.md").write_text("x\n")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "a")

    def test_exports_committed_tree_only(self):
        (self.repo / "bridge" / "src" / "a.ts").write_text("export const a = 2; // 没提交\n")
        tree, ver, src = build_bridge.export_source(self.repo, "HEAD", self.tmp / "work")
        self.assertEqual(tree, git(self.repo, "rev-parse", "HEAD:bridge").strip())
        self.assertEqual(ver, "0.7.1")
        self.assertEqual((src / "src" / "a.ts").read_text(), "export const a = 1;\n")  # 只用已提交的内容
        self.assertFalse((src / "README.md").exists())  # 只有 bridge/ 里的东西
        # 再导一次会先清空
        (src / "stale.txt").write_text("x")
        build_bridge.export_source(self.repo, "HEAD", self.tmp / "work")
        self.assertFalse((src / "stale.txt").exists())

    def test_tree_id_follows_source_only(self):
        t1 = build_bridge.source_tree(self.repo, "HEAD")
        (self.repo / "README.md").write_text("y\n")
        git(self.repo, "commit", "-qam", "readme")
        self.assertEqual(build_bridge.source_tree(self.repo, "HEAD"), t1)  # 源码没变，标识不变
        (self.repo / "bridge" / "src" / "a.ts").write_text("export const a = 3;\n")
        git(self.repo, "commit", "-qam", "src")
        self.assertNotEqual(build_bridge.source_tree(self.repo, "HEAD"), t1)

    def test_ref_without_source_refused(self):
        git(self.repo, "rm", "-rqf", "bridge")
        git(self.repo, "commit", "-qm", "rm")
        with self.assertRaises(build_bridge.BuildError):
            build_bridge.source_tree(self.repo, "HEAD")


if __name__ == "__main__":
    unittest.main()
