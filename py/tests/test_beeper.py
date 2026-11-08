"""The Beeper reader: one source per conversation per day, Messenger days the Facebook download
already holds are not doubled, Beeper's own server is left out.

The tables are the real index's (columns and types copied from `.schema` of BeeperTexts/index.db on
2026-11-08, trimmed to the NOT NULL ones the reader touches); the people and words are invented,
since this repository is public. The last test reads the real index and skips without it.

    .venv/bin/python -m unittest tests.test_beeper
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mannaminne as m  # noqa: E402

SCHEMA = """
CREATE TABLE 'threads' ('threadID' VARCHAR NOT NULL PRIMARY KEY, 'accountID' VARCHAR,
  'thread' JSON NOT NULL, 'timestamp' INTEGER DEFAULT 0, is_label INTEGER DEFAULT 0, room_type TEXT);
CREATE TABLE 'participants' ('account_id' TEXT NOT NULL, 'room_id' TEXT NOT NULL, 'id' TEXT NOT NULL,
  'full_name' TEXT DEFAULT NULL, 'is_self' INTEGER DEFAULT NULL, 'is_network_bot' INTEGER DEFAULT NULL,
  PRIMARY KEY('room_id', 'id'));
CREATE TABLE 'mx_room_messages' ('id' INTEGER PRIMARY KEY AUTOINCREMENT, 'roomID' TEXT NOT NULL,
  'eventID' TEXT NOT NULL, 'senderContactID' TEXT NOT NULL, 'timestamp' INTEGER NOT NULL,
  'isEdited' INTEGER NOT NULL, 'isDeleted' INTEGER NOT NULL, 'isEncrypted' INTEGER NOT NULL,
  'type' TEXT NOT NULL, 'hsOrder' INTEGER NOT NULL, 'isSentByMe' INTEGER NOT NULL,
  'message' JSON NOT NULL, 'text_content' TEXT);
CREATE TABLE 'mx_room_messages_fts_content'(id INTEGER PRIMARY KEY, c0);
"""
# 2026-05-23 12:00 and 2026-05-24 09:30 Stockholm time, in ms
D1, D2 = 1779530400000, 1779607800000


def build(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    rooms = [("!dm:beeper.local", "facebookgo.111", {"type": "single", "title": ""}),
             ("!wa:beeper.local", "whatsapp.46700000000", {"type": "group", "title": "Kollektivet"}),
             ("!hq:beeper.local", "hungryserv", {"type": "group", "title": "Beeper Community"})]
    for tid, acct, t in rooms:
        con.execute("INSERT INTO threads (threadID, accountID, thread) VALUES (?,?,?)", (tid, acct, json.dumps(t)))
    con.executemany("INSERT INTO participants (account_id, room_id, id, full_name, is_self) VALUES (?,?,?,?,?)", [
        ("facebookgo.111", "!dm:beeper.local", "@facebookgo_222:beeper.local", "Alva Exempel", 0),
        ("facebookgo.111", "!dm:beeper.local", "@facebookgo_111:beeper.local", "Fredrik Bränström", 1),
        ("whatsapp.46700000000", "!wa:beeper.local", "@whatsapp_lid-9:beeper.local", "Bo Prov", 0)])
    msgs = [("!dm:beeper.local", "@facebookgo_222:beeper.local", D1, 0, "TEXT", "Hej, har du hört från stiftelsen?"),
            ("!dm:beeper.local", "@facebookgo_111:beeper.local", D1 + 60000, 1, "TEXT", "Inte än, jag väntar"),
            ("!dm:beeper.local", "@facebookgo_222:beeper.local", D2, 0, "TEXT", "Redan i nedladdningen"),
            ("!dm:beeper.local", "@facebookgo_222:beeper.local", D2 + 1000, 0, "REACTION", "👍"),
            ("!wa:beeper.local", "@whatsapp_lid-9:beeper.local", D1, 0, "TEXT", "Hyran är betald"),
            ("!hq:beeper.local", "@x:beeper.com", D1, 0, "TEXT", "Welcome to the community")]
    for i, (room, sender, ts, mine, typ, text) in enumerate(msgs, 1):
        con.execute("INSERT INTO mx_room_messages (id, roomID, eventID, senderContactID, timestamp, isEdited, "
                    "isDeleted, isEncrypted, type, hsOrder, isSentByMe, message) VALUES (?,?,?,?,?,0,0,1,?,?,?,'{}')",
                    (i, room, f"$e{i}", sender, ts, typ, i, mine))
        con.execute("INSERT INTO mx_room_messages_fts_content (id, c0) VALUES (?,?)", (i, text))
    con.commit()
    return con


class BeeperDays(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.con = build(os.path.join(self.dir.name, "index.db"))

    def tearDown(self):
        self.con.close()
        self.dir.cleanup()

    def test_one_source_per_room_and_day_with_names_and_networks(self):
        days, rooms = m._beeper_days(self.con)
        self.assertEqual(sorted((rooms[r]["network"], d) for r, d in days),
                         [("messenger", "2026-05-23"), ("messenger", "2026-05-24"), ("whatsapp", "2026-05-23")])
        said = [w for _, w, _ in days[("!dm:beeper.local", "2026-05-23")]]
        self.assertEqual(said, ["12:00 Alva Exempel", "12:01 Fredrik Bränström"])
        self.assertEqual(rooms["!dm:beeper.local"]["title"], "Alva Exempel")
        self.assertEqual(rooms["!wa:beeper.local"]["title"], "Kollektivet")

    def test_beepers_own_server_and_reactions_are_left_out(self):
        days, rooms = m._beeper_days(self.con)
        self.assertNotIn("!hq:beeper.local", rooms)
        self.assertNotIn("👍", " ".join(t for msgs in days.values() for _, _, t in msgs))

    def test_a_messenger_message_the_download_holds_is_not_doubled(self):
        days, _ = m._beeper_days(self.con, known={("2026-05-24", "Redan i nedladdningen"),
                                                  ("2026-05-23", "Hyran är betald")})
        self.assertNotIn(("!dm:beeper.local", "2026-05-24"), days)
        self.assertIn(("!wa:beeper.local", "2026-05-23"), days)   # only Messenger is checked

    def test_discover_reads_a_copy_and_yields_rows(self):
        self.con.close()
        os.environ["MANNAMINNE_BEEPER_INDEX"] = os.path.join(self.dir.name, "index.db")
        kept = (m._skip_if_unchanged, m._messenger_said_days)
        m._skip_if_unchanged, m._messenger_said_days = (lambda *a, **k: None), (lambda: set())
        try:
            rows = list(m.discover_beeper())
        finally:
            m._skip_if_unchanged, m._messenger_said_days = kept
            os.environ.pop("MANNAMINNE_BEEPER_INDEX")
            self.con = sqlite3.connect(":memory:")
        ids = {r[2] for r in rows}
        self.assertIn("beeper:!dm:beeper.local:2026-05-23", ids)
        self.assertTrue(all(r[1] == "beeper" for r in rows))
        self.assertIn("Messenger · Alva Exempel · 2026-05-23", [r[5] for r in rows])


@unittest.skipUnless(os.path.exists(m.BEEPER_INDEX), "no Beeper index on this machine")
class RealIndex(unittest.TestCase):
    def test_the_real_index_yields_thousands_of_days_across_networks(self):
        kept = m._skip_if_unchanged
        m._skip_if_unchanged = lambda *a, **k: None
        try:
            rows = list(m.discover_beeper())
        finally:
            m._skip_if_unchanged = kept
        self.assertGreater(len({r[2] for r in rows}), 1000)
        self.assertGreaterEqual(len({r[4] for r in rows}), 3)


if __name__ == "__main__":
    unittest.main()
