from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKES = REPO / "tests" / "fakes"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class TicketdTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ticketd-test-")).resolve()
        self._saved_env = dict(os.environ)
        # 在 worker 里跑测试时会继承这两个变量：不清掉的话，测试里的 `ticket report` 会写进真实工单的待收件
        for k in ("TICKETD_SPOOL", "TICKETD_TICKET"):
            os.environ.pop(k, None)
        os.environ.update({
            "TICKETD_HOME": str(self.tmp / "home"),
            "TICKETD_WORKROOT": str(self.tmp / "work"),
            "TICKETD_OWNER": "ou_owner",
            "TICKETD_CLAUDE": str(FAKES / "fake_claude.py"),
            "TICKETD_LARK": str(FAKES / "fake_lark.py"),
            "FAKE_LARK_LOG": str(self.tmp / "lark.log"),
            "TICKETD_RETRY_DELAYS": "0,0,0",
            "TICKETD_MAX_WORKERS": "3",
            "TICKETD_TURN_TIMEOUT": "7200",
        })
        from ticketd import db
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def new_ticket(self, request="做个测试 [[fake:ready]]", message="om_1", **kw):
        from ticketd import core
        args = dict(chat_id="oc_1", message_id=message, title="测试", slug="test",
                    request_text=request, ack_text="开工：动测试文件，预期得到结果")
        args.update(kw)
        return core.create_ticket(self.con, **args)

    def wait(self, cond, sched=None, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if sched is not None:
                sched.tick()
            if cond():
                return
            time.sleep(0.1)
        self.fail("条件在 %ss 内未满足" % timeout)
