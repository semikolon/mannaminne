"""The embed queue can be narrowed to chosen projects and source kinds.

Why it exists: from 2026-09-05 the whole queue was paused to spare the router an email backlog of
several hundred thousand chunks, and the notes and code in daily use were starved along with it, so
search by meaning went blind on them. A scoped run embeds those first and finishes when THEY are done.
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mannaminne as m  # noqa: E402


class FakeCur:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def executemany(self, *a):
        pass

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.cur = FakeCur()

    def cursor(self):
        return self.cur

    def commit(self):
        pass


class EmbedScope(unittest.TestCase):
    def test_no_scope_narrows_nothing(self):
        self.assertEqual(m._embed_scope(None, None), ("", []))

    def test_projects_only(self):
        sql, params = m._embed_scope(["deliberus"], None)
        self.assertEqual(sql, " AND c.project = ANY(%s)")
        self.assertEqual(params, [["deliberus"]])

    def test_projects_and_kinds_keep_the_order_of_their_placeholders(self):
        sql, params = m._embed_scope(["deliberus", "nit"], ["doc", "code"])
        self.assertLess(sql.index("c.project"), sql.index("c.source_kind"))
        self.assertEqual(params, [["deliberus", "nit"], ["doc", "code"]])

    def test_an_empty_list_is_refused_and_never_read_as_everything(self):
        with self.assertRaises(SystemExit):
            m._embed_scope([], None)
        with self.assertRaises(SystemExit):
            m._embed_scope(None, [])

    def test_a_scoped_run_counts_and_selects_only_its_own_chunks(self):
        conn = FakeConn()
        args = types.SimpleNamespace(limit=0, project=["deliberus"], kind=["doc"])
        with mock.patch.object(m, "load_conn", return_value=conn), \
                mock.patch.object(m, "_ensure_email_class"):
            m.cmd_embed(args)
        counts = [(s, p) for s, p in conn.cur.calls
                  if s.startswith("SELECT count(*) FROM chunks c WHERE c.embedding IS NULL")]
        selects = [(s, p) for s, p in conn.cur.calls if s.startswith("SELECT c.id,c.text")]
        self.assertEqual(len(counts), 1)
        self.assertEqual(len(selects), 1)
        scope = "c.project = ANY(%s) AND c.source_kind = ANY(%s)"
        self.assertIn(scope, counts[0][0])
        self.assertEqual(list(counts[0][1]), [["deliberus"], ["doc"]])
        sql, params = selects[0]
        self.assertIn(scope, sql)
        self.assertEqual(list(params[:2]), [["deliberus"], ["doc"]])
        self.assertEqual(sql.count("%s"), len(params))

    def test_an_unscoped_run_is_unchanged(self):
        conn = FakeConn()
        with mock.patch.object(m, "load_conn", return_value=conn), \
                mock.patch.object(m, "_ensure_email_class"):
            m.cmd_embed(types.SimpleNamespace(limit=0))
        sql, params = [(s, p) for s, p in conn.cur.calls if s.startswith("SELECT c.id,c.text")][0]
        self.assertNotIn("c.project", sql)
        self.assertNotIn("c.source_kind", sql)
        self.assertEqual(sql.count("%s"), len(params))


if __name__ == "__main__":
    unittest.main()
