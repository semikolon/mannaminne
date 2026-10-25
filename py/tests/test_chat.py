"""`chat` from Adium's logs and Skype's databases. The markup and the database columns are
copied from the real archive (an Adium ICQ log of 2010, a Skype main.db of 2012-2014); the
words are replaced, because the messages are other people's and this repository is public.
The last test reads the real archive when it is on this machine and skips otherwise."""

import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mannaminne as m  # noqa: E402

HEAD = ('<?xml version="1.0" encoding="UTF-8" ?>\n<chat xmlns="http://purl.org/net/ulf/ns/0.4-02" '
        'account="111" service="ICQ"><event type="windowOpened" sender="111" '
        'time="2010-04-10T07:17:40+02:00"/>\n')
SPAN = '<div><span style="color: #000000; font-family: Helvetica; font-size: 12pt;">{}</span></div>'


def msg(sender, time, text, alias=None):
    a = f' alias="{alias}"' if alias else ""
    return f'<message sender="{sender}" time="{time}"{a}>{SPAN.format(text)}</message>\n'


def write_log(root, service_dir, contact, stamp, body):
    d = Path(root, "adium", "Logs", service_dir, contact, f"{contact} ({stamp}).chatlog")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{contact} ({stamp}).xml").write_text(body, encoding="utf-8")


SKYPE_SCHEMA = ("CREATE TABLE Conversations (id INTEGER PRIMARY KEY, identity TEXT, displayname TEXT);"
                "CREATE TABLE Messages (id INTEGER PRIMARY KEY, convo_id INTEGER, author TEXT, "
                "from_dispname TEXT, timestamp INTEGER, type INTEGER, body_xml TEXT);")


def write_skype(path, rows, convo_id=24):
    con = sqlite3.connect(path)
    con.executescript(SKYPE_SCHEMA)
    con.execute("INSERT INTO Conversations VALUES (?, 'friend', 'A Friend')", (convo_id,))
    con.executemany("INSERT INTO Messages (convo_id, author, from_dispname, timestamp, type, body_xml) "
                    "VALUES (?, ?, ?, ?, ?, ?)", [(convo_id, *r) for r in rows])
    con.commit()
    con.close()


class Chat(unittest.TestCase):
    def rows(self, root):
        old = m.CHAT_ARCHIVE
        m.CHAT_ARCHIVE = root
        try:
            with mock_skip():
                return list(m.discover_chat())
        finally:
            m.CHAT_ARCHIVE = old

    def test_one_source_per_contact_per_day_named_by_the_other_side(self):
        with tempfile.TemporaryDirectory() as d:
            write_log(d, "ICQ.111", "222", "2010-04-10T07.17.40+0200", HEAD
                      + msg("222", "2010-04-10T07:17:54+02:00", "hello there")
                      + msg("111", "2010-04-10T07:18:10+02:00", "It worked!", "Me")
                      + msg("222", "2010-04-11T00:02:00+02:00", "past midnight", "A Friend")
                      + "</chat>")
            rows = self.rows(d)
        self.assertEqual([r[5] for r in rows], ["Adium ICQ · A Friend · 2010-04-10",
                                                "Adium ICQ · A Friend · 2010-04-11"])
        self.assertEqual([r[7] for r in rows], ["2010-04-10", "2010-04-11"])
        self.assertEqual(rows[0][4], "icq")
        self.assertIn("07:18 Me: It worked!", rows[0][6])

    def test_a_log_with_a_second_log_inside_it_and_a_pasted_tail_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            body = (HEAD + msg("222", "2010-03-08T17:30:01+01:00", "first part", "A Friend")
                    + HEAD + msg("222", "2010-03-08T18:00:00+01:00", "second part", "A Friend")
                    + "</chat>\n6:02pmA Friend\nOk, later.\n")
            write_log(d, "Facebook.me@example.com", "333", "2010-03-08T17.30.01+0100", body)
            err = io.StringIO()
            with redirect_stderr(err):
                rows = self.rows(d)
        text = rows[0][6]
        self.assertIn("first part", text)
        self.assertIn("second part", text)
        self.assertIn("(pasted after the log): 6:02pmA Friend / Ok, later.", text)
        self.assertEqual(rows[0][4], "facebook-chat")
        self.assertEqual(err.getvalue(), "")

    def test_skype_messages_are_kept_once_across_overlapping_databases(self):
        ts = 1351965338  # 2012-11-03 18:55:38 in Stockholm
        shared = [("friend", "A Friend", ts, 61, "it&apos;s <ss type=\"smile\">:)</ss> fine"),
                  ("friend", "A Friend", ts + 60, 30, "<partlist alt=\"\"></partlist>")]
        with tempfile.TemporaryDirectory() as d:
            Path(d, "skype").mkdir()
            write_skype(Path(d, "skype", "a_main.db"), shared)
            write_skype(Path(d, "skype", "b_main.db"),
                        shared + [("me", "Me", ts + 120, 61, "and more")], convo_id=7)
            rows = self.rows(d)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r[1], r[2], r[4], r[5], r[7]), (
            "chat", "chat:skype:friend:2012-11-03", "skype", "Skype · A Friend · 2012-11-03",
            "2012-11-03"))
        self.assertEqual(r[6].count("it's :) fine"), 1)
        self.assertIn("18:56 A Friend: [call started]", r[6])
        self.assertIn("18:57 Me: and more", r[6])


class mock_skip:
    def __enter__(self):
        self.old = m._skip_if_unchanged
        m._skip_if_unchanged = lambda kind, paths: None

    def __exit__(self, *exc):
        m._skip_if_unchanged = self.old


@unittest.skipUnless(os.path.isdir(os.path.join(m.CHAT_ARCHIVE, "adium", "Logs")),
                     "the chat archive is not on this machine")
class RealArchive(unittest.TestCase):
    def test_every_log_and_database_is_read(self):
        err = io.StringIO()
        with mock_skip(), redirect_stderr(err):
            rows = list(m.discover_chat())
        sources = {r[2] for r in rows}
        self.assertEqual(err.getvalue(), "")                       # no unreadable log or database
        self.assertGreater(len({s for s in sources if s.startswith("chat:adium:")}), 700)
        self.assertGreater(len({s for s in sources if s.startswith("chat:skype:")}), 800)
        self.assertEqual(len(rows), len({r[0] for r in rows}))    # no chunk twice
        self.assertTrue(all("2009-01-01" <= r[7] <= "2019-12-31" for r in rows))


if __name__ == "__main__":
    unittest.main()
