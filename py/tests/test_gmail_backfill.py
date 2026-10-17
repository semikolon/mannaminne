"""gmail_backfill: personal mail from before the live floor, indexed once."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gmail_backfill as gb  # noqa: E402
import gmail_sweep as gs  # noqa: E402
import mannaminne  # noqa: E402

RAW = b"From: a@b.se\r\nSubject: Hej\r\nMessage-ID: <x@y>\r\nDate: Mon, 1 Mar 2010 10:00:00 +0100\r\n\r\nText.\r\n"


class NoWait:
    def wait(self):
        pass

    def back_off(self, seconds=0):
        pass


def fake_conn(rebuilding=False):
    cur = mock.MagicMock()
    cur.fetchone.return_value = (rebuilding,)
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


class Backfill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "eml").mkdir()
        (self.dir / "eml" / "c1.eml").write_bytes(RAW)
        self.copies = {"c1": {"path": "eml/c1.eml"}}
        self.done = self.dir / "done.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def run_backfill(self, ids, fetch, conn=None):
        fetched = []

        def f(mid):
            fetched.append(mid)
            return fetch(mid)
        with mock.patch.object(mannaminne, "_upsert") as up, \
             mock.patch.object(mannaminne, "_ensure_email_class"):
            gb.backfill(ids, conn or fake_conn(), self.copies, self.dir, done_path=self.done,
                        log=lambda *_: None, fetch=f, pace=NoWait(), workers=2)
        return fetched, up

    def test_a_sweep_copy_is_read_from_disk_not_fetched(self):
        fetched, up = self.run_backfill(["c1", "m2"], lambda m: ("ok", RAW, 0))
        self.assertEqual(fetched, ["m2"])
        self.assertTrue(up.called)
        self.assertEqual(gs.load_ids(self.done), ["c1", "m2"])

    def test_a_second_run_does_only_what_is_left(self):
        self.run_backfill(["c1"], lambda m: ("ok", RAW, 0))
        fetched, _ = self.run_backfill(["c1", "m2"], lambda m: ("ok", RAW, 0))
        self.assertEqual(fetched, ["m2"])

    def test_it_will_not_write_during_a_table_rebuild(self):
        with self.assertRaises(RuntimeError):
            self.run_backfill(["m1"], lambda m: ("ok", RAW, 0), conn=fake_conn(rebuilding=True))

    def test_a_gone_message_is_counted_and_skipped(self):
        fetched, _ = self.run_backfill(["m1"], lambda m: ("gone",))
        self.assertEqual(gs.load_ids(self.done), ["m1"])


if __name__ == "__main__":
    unittest.main()
