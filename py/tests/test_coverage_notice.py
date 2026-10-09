"""A search says so when part of what it covered cannot be found by meaning.

Why it exists: from 2026-09-05 to 2026-10-09 the newest notes had no embeddings, every search over
them fell back to shared words, and nothing said so. The note that mattered most that week had none.
"""
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mannaminne as m  # noqa: E402


def search_args(**over):
    base = dict(query=["who", "weighs", "a", "link"], keyword=False, limit=3, pretty=False)
    base.update(over)
    return types.SimpleNamespace(**base)


class CoverageNotice(unittest.TestCase):
    def test_silent_when_everything_in_scope_is_embedded(self):
        self.assertIsNone(m._coverage_notice([("doc", 0, 22675), ("code", 0, 5181)]))
        self.assertIsNone(m._coverage_notice([]))

    def test_names_each_kind_with_its_counts_worst_first(self):
        note = m._coverage_notice([("code", 3606, 5181), ("doc", 9702, 22675), ("note", 0, 1534)])
        self.assertIn("PARTIAL", note)
        self.assertIn("doc 9,702 of 22,675 (43%)", note)
        self.assertIn("code 3,606 of 5,181 (70%)", note)
        self.assertLess(note.index("doc 9,702"), note.index("code 3,606"))
        self.assertNotIn("note 0", note)

    def test_a_count_that_is_late_or_fails_is_said_and_never_passed_over(self):
        self.assertIn("could not check", m._coverage_notice(late=True))
        failed = m._coverage_notice(error="connection refused")
        self.assertIn("could not check", failed)
        self.assertIn("connection refused", failed)

    def test_the_count_covers_exactly_what_the_search_covers(self):
        sql, params = m._coverage_sql(None, None)
        self.assertTrue(sql.endswith("FROM chunks GROUP BY 1"))   # no narrowing: every kind is counted
        self.assertEqual(params, [])
        sql, params = m._coverage_sql(["doc"], ["deliberus"])
        self.assertIn("WHERE source_kind = ANY(%s) AND project = ANY(%s)", sql)
        self.assertEqual(params, [["doc"], ["deliberus"]])
        self.assertEqual(sql.count("%s"), len(params))

    def test_a_search_puts_the_notice_on_stderr_and_leaves_stdout_alone(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(m, "_scope", return_value=["doc"]), \
                mock.patch.object(m, "_project_scope", return_value=["deliberus"]), \
                mock.patch.object(m, "search_results", return_value=[]), \
                mock.patch.object(m, "_embedding_coverage", return_value=[("doc", 9702, 22675)]) as count, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.cmd_search(search_args())
        count.assert_called_once_with(["doc"], ["deliberus"])
        self.assertEqual(out.getvalue(), "")
        self.assertIn("doc 9,702 of 22,675 (43%)", err.getvalue())

    def test_a_search_by_words_alone_does_not_count(self):
        err = io.StringIO()
        with mock.patch.object(m, "_scope", return_value=None), \
                mock.patch.object(m, "_project_scope", return_value=None), \
                mock.patch.object(m, "search_results", return_value=[]), \
                mock.patch.object(m, "_embedding_coverage") as count, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            m.cmd_search(search_args(keyword=True))
        count.assert_not_called()
        self.assertEqual(err.getvalue(), "")

    def test_a_failing_count_does_not_break_the_search(self):
        err = io.StringIO()
        with mock.patch.object(m, "_scope", return_value=None), \
                mock.patch.object(m, "_project_scope", return_value=None), \
                mock.patch.object(m, "search_results", return_value=[]), \
                mock.patch.object(m, "_embedding_coverage", side_effect=RuntimeError("no route to host")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            m.cmd_search(search_args())
        self.assertIn("could not check", err.getvalue())
        self.assertIn("no route to host", err.getvalue())


if __name__ == "__main__":
    unittest.main()
