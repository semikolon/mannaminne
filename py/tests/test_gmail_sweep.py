"""gmail_sweep: the copy-then-trash run for swept bulk mail. Nothing here talks
to Gmail or the database; every external answer is a recorded or fake one."""

import base64
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gmail_sweep as gs  # noqa: E402
import inbox_messages as im  # noqa: E402

# gws's answer to a message that does not exist, copied from a real run (2026-10-17).
NOT_FOUND = """{
  "error": {
    "code": 404,
    "message": "Requested entity was not found.",
    "reason": "notFound"
  }
}
"""
# gws's answer when the per-minute quota is spent; reason and message as seen on
# 2026-10-17 after 117 raw fetches in 19 seconds.
RATE_LIMITED = json.dumps({"error": {"code": 429, "reason": "rateLimitExceeded", "message":
    "Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user'"}})

RAW = b"From: Ny Teknik <news@nyteknik.se>\r\nSubject: Veckans nyheter\r\nMessage-ID: <abc@x>\r\n" \
      b"Date: Tue, 01 Mar 2022 10:00:00 +0100\r\n\r\nHej, h\xc3\xa4r \xc3\xa4r veckans brev.\r\n"


def ok_answer(raw=RAW, internal=1646125200000):
    return json.dumps({"id": "x", "raw": base64.urlsafe_b64encode(raw).decode(),
                       "internalDate": str(internal)})


def row(**kw):
    r = {"cat": "promotions", "unread": True, "sent": False, "convo": False, "starred": False,
         "important": False, "attach": False, "authority": False, "transaction": False,
         "purchase": False, "human": False, "labels": []}
    r.update(kw)
    return r


class NoWait:
    def wait(self):
        pass

    def back_off(self, seconds=0):
        self.backed_off = True


class Classify(unittest.TestCase):
    def test_a_raw_message_decodes_to_its_bytes(self):
        kind, raw, internal = gs.classify(0, ok_answer())
        self.assertEqual((kind, raw, internal), ("ok", RAW, 1646125200000))

    def test_not_found_is_gone_not_an_error(self):
        self.assertEqual(gs.classify(1, "Using keyring backend: keyring\n" + NOT_FOUND), ("gone",))

    def test_the_spent_quota_is_a_rate_limit(self):
        self.assertEqual(gs.classify(1, RATE_LIMITED), ("rate",))

    def test_a_refused_login_stops_the_run(self):
        self.assertEqual(gs.classify(1, json.dumps({"error": {"code": 401, "message": "no"}}))[0], "auth")

    def test_garbage_is_an_error(self):
        self.assertEqual(gs.classify(1, "segfault")[0], "error")


class Plan(unittest.TestCase):
    def test_the_plan_drops_what_changed_what_is_gone_and_what_is_cited(self):
        approved = {"a": row(), "b": row(), "c": row(), "d": row(), "e": row(cat="personal")}
        fresh = {"a": row(), "b": row(starred=True), "d": row(), "e": row(cat="personal"),
                 "new": row()}                       # c deleted since; new arrived since
        plan, s = gs.make_plan(approved, fresh, [], cited_gmail={"d"})
        self.assertEqual(plan, ["a"])
        self.assertEqual((s["approved_2026_10_08"], s["no_longer_in_sweep"], s["gone_from_gmail"],
                          s["cited_in_docs"]), (4, 1, 1, 1))

    def test_mail_that_arrived_after_the_approved_inventory_is_not_swept(self):
        plan, _ = gs.make_plan({}, {"new": row()}, [], set())
        self.assertEqual(plan, [])


class Copy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_copy(self, plan, answers, **kw):
        calls = []

        def fetch(mid):
            calls.append(mid)
            seq = answers[mid]
            return seq.pop(0) if len(seq) > 1 else seq[0]
        code = gs.copy(plan, self.dest, fetch=fetch, pace=NoWait(), workers=2, log=lambda *_: None,
                       source_id=lambda raw: "email:" + "0" * 16, **kw)
        return code, calls

    def test_each_message_is_written_once_and_recorded(self):
        ok = gs.classify(0, ok_answer())
        code, _ = self.run_copy(["m1", "m2"], {"m1": [ok], "m2": [ok]})
        self.assertEqual(code, (0, "done"))
        rec = gs.load_copied(self.dest)
        self.assertEqual(set(rec), {"m1", "m2"})
        self.assertEqual((self.dest / rec["m1"]["path"]).read_bytes(), RAW)
        self.assertEqual(rec["m1"]["path"], "eml/2022-03/m1.eml")

    def test_a_second_run_fetches_nothing_already_copied(self):
        ok = gs.classify(0, ok_answer())
        self.run_copy(["m1"], {"m1": [ok]})
        _, calls = self.run_copy(["m1", "m2"], {"m1": [ok], "m2": [ok]})
        self.assertEqual(calls, ["m2"])

    def test_a_rate_limit_waits_and_retries_the_same_message(self):
        ok = gs.classify(0, ok_answer())
        code, calls = self.run_copy(["m1"], {"m1": [("rate",), ok]})
        self.assertEqual((code, calls), ((0, "done"), ["m1", "m1"]))
        self.assertIn("m1", gs.load_copied(self.dest))

    def test_a_message_deleted_since_the_plan_is_recorded_as_gone(self):
        self.run_copy(["m1"], {"m1": [("gone",)]})
        self.assertEqual(gs.load_ids(self.dest / "gone.tsv"), ["m1"])

    def test_a_refused_login_stops_everything(self):
        code, _ = self.run_copy(["m1"], {"m1": [("auth", "expired")]})
        self.assertEqual(code[0], 3)

    def test_a_run_of_failures_stops_the_copy(self):
        with mock.patch.object(gs.time, "sleep"):
            code, _ = self.run_copy([f"m{i}" for i in range(6)],
                                    {f"m{i}": [("error", "boom")] for i in range(6)},
                                    max_errors=1, stop_after_failures=3)
        self.assertEqual(code[0], 4)


class Pace(unittest.TestCase):
    def test_starts_are_spaced_and_a_back_off_is_shared(self):
        now = [0.0]
        slept = []
        p = gs.Pace(60, clock=lambda: now[0], sleep=lambda d: slept.append(d))
        p.wait()
        p.wait()
        self.assertEqual(slept, [1.0])
        p.back_off(30)
        p.wait()
        self.assertEqual(slept[-1], 30.0)


class Listing(unittest.TestCase):
    def test_a_failed_page_is_retried_not_taken_for_the_end(self):
        bad = types.SimpleNamespace(returncode=1, stdout=RATE_LIMITED + "\n", stderr="")
        good = types.SimpleNamespace(returncode=0, stdout='{"messages":[{"id":"a"}]}\n', stderr="")
        with mock.patch.object(im.subprocess, "run", side_effect=[bad, good]):
            self.assertEqual(im._gws(["x"], sleep=lambda s: None), ['{"messages":[{"id":"a"}]}'])

    def test_a_listing_that_keeps_failing_raises(self):
        bad = types.SimpleNamespace(returncode=1, stdout=RATE_LIMITED, stderr="")
        with mock.patch.object(im.subprocess, "run", return_value=bad):
            with self.assertRaises(RuntimeError):
                im._gws(["x"], attempts=2, sleep=lambda s: None)

    def test_an_error_answer_never_reads_as_no_labels(self):
        bad = types.SimpleNamespace(returncode=1, stdout=NOT_FOUND, stderr="")
        with mock.patch.object(im.subprocess, "run", return_value=bad):
            with self.assertRaises(RuntimeError):
                im.user_labels()


class Trash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name)
        (self.dest / "copied.tsv").write_text(
            "".join(f"{m}\t0\t1\tsha\temail:{m}\teml/{m}.eml\n" for m in ("a", "b", "c")))

    def tearDown(self):
        self.tmp.cleanup()

    def write_check(self, **kw):
        s = {"not_copied": 0, "damaged": 0, "missing_from_index": 0}
        s.update(kw)
        (self.dest / "check.json").write_text(json.dumps(s))

    def test_no_check_no_trash(self):
        with self.assertRaises(RuntimeError):
            gs.trash_eligible(self.dest, ["a"])

    def test_a_message_the_index_lacks_blocks_the_trash(self):
        self.write_check(missing_from_index=2)
        with self.assertRaises(RuntimeError):
            gs.trash_eligible(self.dest, ["a"])

    def test_cited_and_already_trashed_messages_are_left(self):
        self.write_check()
        (self.dest / "spare.tsv").write_text("b\n")
        (self.dest / "trashed.tsv").write_text("c\n")
        self.assertEqual(gs.trash_eligible(self.dest, ["a", "b", "c", "never-copied"]), ["a"])

    def test_one_message_is_read_back_before_the_rest(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            if "batchModify" in argv:
                return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
            return types.SimpleNamespace(returncode=0, stdout='{"labelIds":["TRASH"]}', stderr="")
        gs.trash(["a", "b", "c"], self.dest, run=run, log=lambda *_: None, sleep=lambda s: None)
        bodies = [json.loads(a[a.index("--json") + 1]) for a in calls if "batchModify" in a]
        self.assertEqual([b["ids"] for b in bodies], [["a"], ["b", "c"]])
        self.assertEqual(gs.load_ids(self.dest / "trashed.tsv"), ["a", "b", "c"])

    def test_a_message_that_does_not_reach_the_trash_stops_the_run(self):
        def run(argv, **kw):
            return types.SimpleNamespace(returncode=0, stdout='{"labelIds":["INBOX"]}', stderr="")
        with self.assertRaises(RuntimeError):
            gs.trash(["a", "b"], self.dest, run=run, log=lambda *_: None)
        self.assertEqual(gs.load_ids(self.dest / "trashed.tsv"), [])

    def test_the_backup_must_hold_every_copy(self):
        lines = "\n".join(json.dumps({"type": "file", "path": f"/x/eml/2022-03/{m}.eml"})
                          for m in ("a", "b")) + "\n" + json.dumps({"type": "dir", "path": "/x/eml"})
        run = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=lines, stderr="")  # noqa: E731
        self.assertTrue(gs.backup_holds_copies(Path("/x"), 2, run=run)[0])
        self.assertFalse(gs.backup_holds_copies(Path("/x"), 3, run=run)[0])

    def test_the_backup_listing_is_recursive(self):
        # Without --recursive, restic lists only the top folder and every copy reads as missing
        # (found 2026-10-18: the gate waited on 0 of 71,113 copies the snapshot held).
        seen = []
        def run(argv, **kw):
            seen.append(argv[-1])
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        gs.backup_holds_copies(Path("/x"), 1, run=run)
        self.assertIn("ls latest --recursive", seen[0])


class Check(unittest.TestCase):
    def test_a_copy_whose_index_id_is_absent_is_missing(self):
        with tempfile.TemporaryDirectory() as t:
            dest = Path(t)
            (dest / "eml").mkdir()
            import hashlib
            sha = hashlib.sha256(RAW).hexdigest()
            for m in ("a", "b"):
                (dest / "eml" / f"{m}.eml").write_bytes(RAW)
            (dest / "copied.tsv").write_text(
                f"a\t0\t1\t{sha}\temail:aaaa\teml/a.eml\nb\t0\t1\t{sha}\temail:bbbb\teml/b.eml\n")
            cur = mock.MagicMock()
            cur.fetchall.return_value = [("email:aaaa",)]
            conn = mock.MagicMock()
            conn.cursor.return_value.__enter__.return_value = cur
            s = gs.check(dest, ["a", "b"], conn, cited_index={"aaaa"})
            self.assertEqual((s["missing_from_index"], s["damaged"], s["cited_by_index_id"]), (1, 0, 1))
            self.assertEqual(gs.load_ids(dest / "missing.tsv"), ["b"])


if __name__ == "__main__":
    unittest.main()


class Finish(unittest.TestCase):
    def test_a_failed_copy_stops_before_anything_is_trashed_and_says_so(self):
        pushed = []
        with mock.patch.object(gs, "cmd_copy", return_value=4), \
             mock.patch.object(gs, "notify", side_effect=lambda t, b: pushed.append((t, b))), \
             mock.patch.object(gs, "trash") as trash:
            code = gs.cmd_finish(types.SimpleNamespace(workers=1, backup_waits=1))
        self.assertEqual(code, 1)
        trash.assert_not_called()
        self.assertIn("stannade", pushed[0][0])
        self.assertIn("Inget har flyttats", pushed[0][1])

    def test_no_topic_no_push(self):
        run = mock.MagicMock()
        with mock.patch.object(gs, "NTFY_URL", ""):
            gs.notify("t", "b", run=run)
        run.assert_not_called()
