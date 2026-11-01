from __future__ import annotations

from helpers import TicketdTestCase


class DbTest(TicketdTestCase):
    def test_schema_created(self):
        names = {r["name"] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"tickets", "grants", "bindings", "inputs", "reports", "outbox", "locks"} <= names)

    def test_wal_mode(self):
        mode = self.con.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")

    def test_tx_rolls_back(self):
        from ticketd import db
        with self.assertRaises(RuntimeError):
            with db.tx(self.con):
                self.con.execute("INSERT INTO locks VALUES('/x','T0001')")
                raise RuntimeError("boom")
        self.assertIsNone(self.con.execute("SELECT * FROM locks").fetchone())
