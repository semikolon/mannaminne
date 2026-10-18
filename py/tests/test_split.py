"""Mail is split from everything else, and a plain search leaves it out.

Why it exists: on 2026-10-17 `chunks` became a table split by source kind (`chunks_mail`,
`chunks_rest`), so a plain search reads only the part that fits in Darwin's memory. These tests
hold the three things that depend on it: which kinds a search leaves out, that the upsert names
the split table's key, and that the vector search orders by the expression the index is built on.
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mannaminne as m  # noqa: E402

KINDS = ("session", "doc", "messenger", "aichat", "note", "email",
         "things3", "fyr", "screenshot", "git_commit", "code")


def args(**on):
    base = {k: False for k in KINDS}
    base["all"] = False
    base.update(on)
    return types.SimpleNamespace(**base)


class WhatAPlainSearchLeavesOut(unittest.TestCase):
    def setUp(self):
        self._env = m.os.environ.get("MANNAMINNE_INVOKED_AS")
        m.os.environ["MANNAMINNE_INVOKED_AS"] = "mannaminne"

    def tearDown(self):
        if self._env is None:
            m.os.environ.pop("MANNAMINNE_INVOKED_AS", None)
        else:
            m.os.environ["MANNAMINNE_INVOKED_AS"] = self._env

    def test_plain_search_leaves_mail_out(self):
        a = args()
        scope = m._scope(a)
        self.assertIsNone(scope)
        self.assertEqual(m._exclude(a, scope), ["email", "said"])

    def test_naming_mail_searches_mail(self):
        a = args(email=True)
        scope = m._scope(a)
        self.assertEqual(scope, ["email"])
        self.assertIsNone(m._exclude(a, scope))

    def test_all_searches_everything(self):
        a = args(all=True)
        scope = m._scope(a)
        self.assertIsNone(scope)
        self.assertIsNone(m._exclude(a, scope))

    def test_naming_another_kind_needs_no_exclusion(self):
        a = args(doc=True)
        self.assertIsNone(m._exclude(a, m._scope(a)))

    def test_a_new_kind_is_searched_by_default(self):
        # The default is an exclusion, so a kind nobody listed is still searched. The two
        # left out are mail (size) and his own messages (already inside the session rows).
        self.assertEqual(tuple(m.DEFAULT_EXCLUDED_KINDS), ("email", "said"))

    def test_naming_said_searches_his_words_alone(self):
        a = args(said=True)
        scope = m._scope(a)
        self.assertEqual(scope, ["said"])
        self.assertIsNone(m._exclude(a, scope))

    def test_eval_cases_search_what_a_plain_search_searches(self):
        self.assertEqual(m._case_exclude({"query": "x"}), ["email", "said"])
        self.assertIsNone(m._case_exclude({"query": "x", "scope": "doc"}))
        self.assertIsNone(m._case_exclude({"query": "x", "all": True}))

    def test_the_coverage_count_leaves_out_what_the_search_leaves_out(self):
        sql, params = m._coverage_sql(None, None, ["email", "said"])
        self.assertIn("WHERE source_kind <> ALL(%s)", sql)
        self.assertEqual(params, [["email", "said"]])
        self.assertEqual(sql.count("%s"), len(params))


class FakeCursor:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def executemany(self, sql, rows):
        self.sql.append((sql, rows))

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()

    def cursor(self):
        return self.cur

    def close(self):
        pass


class TheSqlMatchesTheSplitTable(unittest.TestCase):
    def test_upsert_names_the_split_tables_key(self):
        cur = FakeCursor()
        m._upsert(cur, [])
        self.assertIn("ON CONFLICT (id, source_kind)", cur.sql[0][0])

    def test_vector_search_orders_by_the_indexed_expression_and_excludes_mail(self):
        conn = FakeConn()
        orig = m._embed_batch
        m._embed_batch = lambda texts, for_query=False: [[0.0] * m.EMBED_DIM]
        try:
            m.search_results("anything", conn=conn, exclude=["email"])
        finally:
            m._embed_batch = orig
        semantic = [(s, p) for s, p in conn.cur.sql if "ORDER BY embedding" in s]
        self.assertEqual(len(semantic), 1)
        sql, params = semantic[0]
        self.assertIn(f"ORDER BY embedding::halfvec({m.EMBED_DIM})<=>%s::halfvec({m.EMBED_DIM})", sql)
        self.assertIn("1-(embedding<=>%s::vector) AS sem", sql)   # scored at full precision
        self.assertIn("source_kind <> ALL(%s)", sql)
        self.assertIn(["email"], params)
        self.assertEqual(sql.count("%s"), len(params))
        for s, p in conn.cur.sql:
            if "FROM chunks" in s:
                self.assertIn("source_kind <> ALL(%s)", s)   # every layer leaves mail out


class FreshSetupBuildsTheSplit(unittest.TestCase):
    def test_setup_creates_the_split_table(self):
        setup = (Path(__file__).resolve().parents[2] / "setup.sh").read_text()
        self.assertIn("PARTITION BY LIST (source_kind)", setup)
        self.assertIn("PARTITION OF chunks FOR VALUES IN ('email')", setup)
        self.assertIn("PRIMARY KEY (id, source_kind)", setup)


if __name__ == "__main__":
    unittest.main()
